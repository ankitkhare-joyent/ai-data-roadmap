"""Activities: the only place this agent touches the outside world (Claude, OpenBao, the fleet).

Two rules make them safe to retry:
  - classify errors: retryable ones are raised as-is, permanent ones as non_retryable ApplicationErrors
  - make side effects idempotent: see fleet.Fleet.stop
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import timedelta

import anthropic
from temporalio import activity
from temporalio.exceptions import ApplicationError

from durable_agent.bao import BaoClient
from durable_agent.fleet import Fleet, FleetError, TransientFleetError
from durable_agent.models import LlmInput, LlmOutput, ToolCall, ToolOutput, ToolRequest
from durable_agent.prompts import TOOLS
from durable_agent.scripted import scripted_reply

log = logging.getLogger("durable_agent.activities")

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
KEY_CACHE_SECONDS = 300


class Activities:
    def __init__(self, bao: BaoClient | None, fleet: Fleet, llm_mode: str = "claude"):
        self.bao, self.fleet, self.llm_mode = bao, fleet, llm_mode
        self._client: anthropic.AsyncAnthropic | None = None
        self._client_at = 0.0

    async def _anthropic(self) -> anthropic.AsyncAnthropic:
        # Re-read the key every few minutes so a rotation in OpenBao takes effect without a redeploy.
        if self._client is None or time.monotonic() - self._client_at > KEY_CACHE_SECONDS:
            data, _ = await self.bao.kv_get("durable-agent/anthropic")
            # max_retries=0: Temporal owns retries, with backoff and a record of every attempt.
            self._client = anthropic.AsyncAnthropic(api_key=data["api_key"], max_retries=0)
            self._client_at = time.monotonic()
        return self._client

    @activity.defn(name="llm_step")
    async def llm_step(self, inp: LlmInput) -> LlmOutput:
        info = activity.info()
        log.info("llm_step workflow=%s attempt=%s messages=%s", info.workflow_id, info.attempt, len(inp.messages))
        if self.llm_mode == "scripted":
            return scripted_reply(inp.messages)

        client = await self._anthropic()
        try:
            resp = await client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=[{"type": "text", "text": inp.system, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,
                messages=inp.messages,
                output_config={"effort": "medium"},
                # If a safety classifier declines, the API re-runs the request on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                extra_body={"fallbacks": "default"},
            )
        except (anthropic.BadRequestError, anthropic.NotFoundError) as e:
            raise ApplicationError(f"bad request: {e.message}", type="BadRequest", non_retryable=True) from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            # Not retried: an operator must fix the key in OpenBao, then reset or restart the workflow.
            raise ApplicationError(f"auth failed: {e.message}", type="AuthFailed", non_retryable=True) from e
        except anthropic.RateLimitError as e:
            wait = int(e.response.headers.get("retry-after", "20"))
            raise ApplicationError("rate limited", type="RateLimited",
                                   next_retry_delay=timedelta(seconds=wait)) from e
        # APIConnectionError, timeouts and 5xx propagate unchanged and Temporal retries them.

        content = [b.to_dict() for b in resp.content]
        calls = [ToolCall(id=b["id"], name=b["name"], input=b["input"]) for b in content if b["type"] == "tool_use"]
        return LlmOutput(content=content, stop_reason=resp.stop_reason or "end_turn", tool_calls=calls,
                         input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)

    @activity.defn(name="run_tool")
    async def run_tool(self, req: ToolRequest) -> ToolOutput:
        call, attempt = req.call, activity.info().attempt
        log.info("run_tool %s key=%s attempt=%s", call.name, req.idempotency_key, attempt)
        try:
            match call.name:
                case "list_idle_instances":
                    out = self.fleet.list_idle(call.input["max_cpu_p95"], call.input["min_idle_days"])
                case "describe_instance":
                    out = self.fleet.describe(call.input["instance_id"])
                case "stop_instance":
                    flaky = os.environ.get("FLEET_FLAKY_STOPS") == "1" and attempt == 1
                    out = self.fleet.stop(call.input["instance_id"], req.idempotency_key, fail_first_attempt=flaky)
                case _:
                    raise ApplicationError(f"unknown tool {call.name}", type="InvalidToolInput", non_retryable=True)
        except TransientFleetError:
            raise                                   # retried by Temporal
        except FleetError as e:
            # A business error is a *result* for the model to reason about, not a failure to retry.
            return ToolOutput(str(e), is_error=True)
        except KeyError as e:
            raise ApplicationError(f"missing input {e}", type="InvalidToolInput", non_retryable=True) from e
        return ToolOutput(json.dumps(out))
