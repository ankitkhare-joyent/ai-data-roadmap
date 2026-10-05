"""An orchestrator that delegates to remote specialist agents over A2A.

Orchestrator-workers is the multi-agent pattern that holds up in production (Anthropic's research system
uses it): a lead agent plans, fans work out to specialists in parallel, resolves their follow-up questions,
and writes the final answer. The orchestrator owns the guard rails:

  budget       a cap on delegations per request (multi-agent systems use several times the tokens of a
               single agent; Anthropic measured about 15x for its research system)
  deadline     every remote call has a timeout; a slow specialist degrades the answer, not the request
  follow-ups   when a specialist answers `input-required`, the orchestrator tries to resolve it from what
               other agents returned before escalating to the user
  trace        one W3C traceparent across every hop, plus a span list you can ship to OpenTelemetry

The planner here is a few rules over the agents' cards (skills and tags), so the lesson runs offline.
With an API key, `ClaudePlanner` lets Claude choose agents, using each card's description as a tool.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from multi_agent.remote import RemoteAgent, Reply, new_traceparent

IID = re.compile(r"i-[0-9a-f]{17}")


@dataclass
class Budget:
    max_delegations: int = 4
    used: int = 0

    def take(self) -> bool:
        if self.used >= self.max_delegations:
            return False
        self.used += 1
        return True


@dataclass
class Result:
    answer: str
    spans: list[Reply] = field(default_factory=list)
    seconds: float = 0.0
    traceparent: str = ""
    delegations: int = 0


class RulePlanner:
    """Pick agents by matching the question against each card's skill tags and examples."""

    def plan(self, question: str, cards: dict[str, object]) -> list[str]:
        q = question.lower()
        wanted = []
        if any(w in q for w in ("fix", "runbook", "remediat", "what should i do")):
            wanted.append("runbook-expert")
        if any(w in q for w in ("idle", "hot", "fleet", "instance", "cpu", "which")):
            wanted.append("fleet-analyst")
        return [w for w in wanted if w in cards]


class Orchestrator:
    def __init__(self, agents: dict[str, str], tokens: dict[str, str], planner=None, budget: Budget | None = None,
                 timeout_s: float = 10):
        self.urls, self.tokens, self.timeout_s = agents, tokens, timeout_s
        self.planner = planner or RulePlanner()
        self.budget_template = budget or Budget()

    async def answer(self, question: str, parallel: bool = True) -> Result:
        t0, trace = time.perf_counter(), new_traceparent()
        budget = Budget(self.budget_template.max_delegations)
        remotes = {n: RemoteAgent(u, self.tokens[n], trace, self.timeout_s) for n, u in self.urls.items()}
        try:
            cards = {}
            for r in remotes.values():
                c = await r.connect()
                cards[c.name] = c
            chosen = self.planner.plan(question, cards)
            by_name = {r.card.name: r for r in remotes.values()}

            async def delegate(name: str) -> Reply:
                if not budget.take():
                    return Reply(agent=name, state="SKIPPED_BUDGET", text="")
                return await by_name[name].ask(question)

            if parallel:
                replies = list(await asyncio.gather(*(delegate(n) for n in chosen)))
            else:
                replies = [await delegate(n) for n in chosen]
            spans = list(replies)

            # Resolve follow-up questions from what the other agents found, before bothering the user.
            for i, rep in enumerate(replies):
                if rep.state == "TASK_STATE_INPUT_REQUIRED" and budget.take():
                    known = IID.findall(question) or [x for o in replies if o is not rep for x in IID.findall(o.text)]
                    if known:
                        hot = _hot_instance(replies) or known[0]
                        follow = await by_name[rep.agent].ask(f"{question} (instance {hot})",
                                                              task_id=rep.task_id, context_id=rep.context_id)
                        spans.append(follow)
                        replies[i] = follow
            answer = _synthesise(question, replies)
            return Result(answer, spans, time.perf_counter() - t0, trace, budget.used)
        finally:
            for r in remotes.values():
                await r.close()


def _hot_instance(replies: list[Reply]) -> str | None:
    for r in replies:
        m = re.search(r"hot \(p95 CPU > 80%\): \['(i-[0-9a-f]{17})'", r.text)
        if m:
            return m.group(1)
    return None


def _synthesise(question: str, replies: list[Reply]) -> str:
    parts = []
    for r in replies:
        if r.state == "TASK_STATE_COMPLETED":
            parts.append(f"[{r.agent}] {r.text}")
        elif r.state == "TASK_STATE_INPUT_REQUIRED":
            parts.append(f"[{r.agent}] needs more information: {r.text}")
        else:
            parts.append(f"[{r.agent}] unavailable ({r.state}); answer may be incomplete")
    return "\n".join(parts) if parts else "No specialist agent covers this question."


class ClaudePlanner:
    """Let Claude pick the agents: each card becomes a tool whose description is the card's description."""

    def __init__(self, model: str = "claude-opus-5-5"):
        import anthropic
        self.client, self.model = anthropic.Anthropic(), model

    def plan(self, question: str, cards: dict[str, object]) -> list[str]:
        tools = [{"name": n.replace("-", "_"), "description": f"{c.description} Skills: "
                  f"{', '.join(s.name for s in c.skills)}.",
                  "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}}
                 for n, c in cards.items()]
        resp = self.client.messages.create(
            model=self.model, max_tokens=2048, tools=tools, output_config={"effort": "low"},
            system="Call every agent whose skills are needed to answer the question. Call none if none fit.",
            messages=[{"role": "user", "content": question}])
        return [b.name.replace("_", "-") for b in resp.content if b.type == "tool_use"]
