"""A deterministic stand-in for Claude, so the lab and the tests run without an API key.

It plays the same three turns a real run usually takes: look for idle instances, ask to stop the
non-prod ones, then summarise. Switch to the real model with LLM_MODE=claude.
"""
from __future__ import annotations

import json

from durable_agent.models import LlmOutput, ToolCall


def _tool_results(messages: list[dict]) -> list[dict]:
    last = messages[-1]
    return [b for b in last["content"] if b.get("type") == "tool_result"] if isinstance(last["content"], list) else []


def scripted_reply(messages: list[dict]) -> LlmOutput:
    assistant_turns = sum(1 for m in messages if m["role"] == "assistant")

    if assistant_turns == 0:
        call = ToolCall(id="toolu_list_1", name="list_idle_instances", input={"max_cpu_p95": 5, "min_idle_days": 7})
        return _tool_turn("Let me find idle instances first.", [call])

    if assistant_turns == 1:
        idle = json.loads(_tool_results(messages)[0]["content"])
        calls = [
            ToolCall(id=f"toolu_stop_{i['instance_id'][-4:]}", name="stop_instance",
                     input={"instance_id": i["instance_id"],
                            "reason": f"{i['env']}, p95 CPU {i['cpu_p95']}% for {i['idle_days']} days"})
            for i in idle if i["env"] != "prod"
        ]
        return _tool_turn(f"{len(calls)} non-prod instances look safe to stop.", calls)

    results = _tool_results(messages)
    stopped = [r for r in results if not r.get("is_error")]
    rejected = [r for r in results if r.get("is_error")]
    text = (f"Done. Stopped {len(stopped)} instance(s); {len(rejected)} not stopped (rejected or failed). "
            "Production instances were left running by policy.")
    return LlmOutput(content=[{"type": "text", "text": text}], stop_reason="end_turn",
                     input_tokens=900, output_tokens=60)


def _tool_turn(text: str, calls: list[ToolCall]) -> LlmOutput:
    content = [{"type": "text", "text": text}] + [
        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
    ]
    return LlmOutput(content=content, stop_reason="tool_use", tool_calls=calls, input_tokens=800, output_tokens=120)
