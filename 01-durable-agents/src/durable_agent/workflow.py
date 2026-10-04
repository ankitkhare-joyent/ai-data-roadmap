"""The agent loop as a Temporal workflow.

Workflow code must be deterministic: on recovery Temporal replays the event history through this code,
and every decision has to come out the same. So the rules are:
  - no network, clock, randomness or file IO here; anything non-deterministic is an activity
  - time comes from workflow.now(), sleeps and timeouts are durable timers
  - the LLM call is an activity, so its result is recorded once and replayed, never re-sampled

What that buys you: a worker can crash between any two steps, a deploy can restart every pod, the
agent can wait three days for a human, and the run carries on from the last recorded step without
calling Claude again for turns it already finished.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from durable_agent.models import (
        RISKY_TOOLS,
        AgentRequest,
        AgentResult,
        AgentStatus,
        Decision,
        LlmInput,
        LlmOutput,
        PendingApproval,
        ToolCall,
        ToolOutput,
        ToolRequest,
    )
    from durable_agent.prompts import SYSTEM_PROMPT

# LLM calls: transient failures (429, 5xx, timeouts) retry with backoff; bad requests do not.
LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=6,
    non_retryable_error_types=["BadRequest", "AuthFailed"],
)
# Tool calls: retried too, which is only safe because each carries an idempotency key.
TOOL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
    non_retryable_error_types=["InvalidToolInput"],
)


@workflow.defn
class CostCleanupAgent:
    def __init__(self) -> None:
        self._phase = "thinking"
        self._turn = 0
        self._pending: dict[str, PendingApproval] = {}
        self._decisions: dict[str, Decision] = {}
        self._llm_calls = 0
        self._in_tokens = 0
        self._out_tokens = 0

    # ------------------------------------------------------------------ main loop
    @workflow.run
    async def run(self, req: AgentRequest) -> AgentResult:
        messages: list[dict] = [{"role": "user", "content": req.goal}]
        actions: list[str] = []

        for self._turn in range(1, req.max_turns + 1):
            self._phase = "thinking"
            step: LlmOutput = await workflow.execute_activity(
                "llm_step",
                LlmInput(messages=messages, system=SYSTEM_PROMPT),
                result_type=LlmOutput,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=LLM_RETRY,
            )
            self._llm_calls += 1
            self._in_tokens += step.input_tokens
            self._out_tokens += step.output_tokens
            # Append the assistant turn exactly as returned (thinking blocks included): never edit history.
            messages.append({"role": "assistant", "content": step.content})

            if step.stop_reason == "refusal":
                return self._result("refused", "The model declined this request.", actions)
            if step.stop_reason != "tool_use":
                answer = "\n".join(b.get("text", "") for b in step.content if b.get("type") == "text").strip()
                return self._result("completed", answer, actions)

            results = await self._run_tools(step.tool_calls, req, actions)
            # All tool results go back in ONE user message, in the order Claude asked for them.
            messages.append({"role": "user", "content": results})

        return self._result("turn_limit", f"Stopped after {req.max_turns} turns without a final answer.", actions)

    async def _run_tools(self, calls: list[ToolCall], req: AgentRequest, actions: list[str]) -> list[dict]:
        # 1. Ask a human about every risky call at once, then wait for all answers (or the timeout).
        risky = [c for c in calls if c.name in RISKY_TOOLS]
        verdicts = await self._await_approvals(risky, timedelta(seconds=req.approval_timeout_s)) if risky else {}

        # 2. Run everything allowed concurrently. Each activity is recorded separately, so a crash halfway
        #    through re-runs only the calls that had not completed.
        self._phase = "running_tools"
        wf_id = workflow.info().workflow_id

        async def one(call: ToolCall) -> dict:
            verdict = verdicts.get(call.id)
            if verdict is not None and not verdict.approved:
                out = ToolOutput(f"Not executed: rejected by {verdict.reviewer}. Reason: {verdict.reason or 'none'}",
                                 is_error=True)
            else:
                out = await workflow.execute_activity(
                    "run_tool",
                    ToolRequest(call=call, idempotency_key=f"{wf_id}/{call.id}"),
                    result_type=ToolOutput,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=TOOL_RETRY,
                )
                if call.name in RISKY_TOOLS and not out.is_error:
                    actions.append(f"{call.name}({call.input}) approved by {verdict.reviewer if verdict else '?'}")
            return {"type": "tool_result", "tool_use_id": call.id, "content": out.content, "is_error": out.is_error}

        return list(await asyncio.gather(*(one(c) for c in calls)))

    async def _await_approvals(self, calls: list[ToolCall], timeout: timedelta) -> dict[str, Decision]:
        self._phase = "awaiting_approval"
        now = workflow.now().isoformat()
        for c in calls:
            self._pending[c.id] = PendingApproval(call=c, asked_at=now)
        ids = [c.id for c in calls]
        try:
            # A durable timer: costs nothing while waiting, survives restarts, fires even if no worker
            # was running at the deadline (it fires when one comes back).
            await workflow.wait_condition(lambda: all(i in self._decisions for i in ids), timeout=timeout)
        except TimeoutError:
            for i in ids:
                self._decisions.setdefault(
                    i, Decision(tool_use_id=i, approved=False, reviewer="timeout",
                                reason=f"no decision within {timeout}"))
        for i in ids:
            self._pending.pop(i, None)
        return {i: self._decisions[i] for i in ids}

    # ------------------------------------------------------------------ human in the loop
    @workflow.update
    async def decide(self, d: Decision) -> str:
        """Approve or reject one pending action. An Update (not a Signal) so the caller gets an answer back."""
        self._decisions[d.tool_use_id] = d
        return f"{'approved' if d.approved else 'rejected'} {self._pending[d.tool_use_id].call.name}"

    @decide.validator
    def _validate_decide(self, d: Decision) -> None:
        # Runs before the update is written to history: invalid calls are refused and leave no trace.
        if d.tool_use_id not in self._pending:
            raise ValueError(f"nothing pending with id {d.tool_use_id}")
        if d.tool_use_id in self._decisions:
            raise ValueError("already decided")
        if not d.reviewer.strip():
            raise ValueError("reviewer is required for the audit trail")

    @workflow.query
    def status(self) -> AgentStatus:
        return AgentStatus(self._phase, self._turn, list(self._pending.values()),
                           self._llm_calls, self._in_tokens, self._out_tokens)

    def _result(self, outcome: str, answer: str, actions: list[str]) -> AgentResult:
        self._phase = "done"
        return AgentResult(outcome, answer, actions, self._llm_calls, self._in_tokens, self._out_tokens)
