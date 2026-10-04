"""Three ways to run the same LLM step over many rows, behind one interface.

  scripted       deterministic keyword rules standing in for a model: offline, free, about 90% accurate
  claude-batch   Anthropic Message Batches API: 50% cheaper, results within 24h (usually far sooner).
                 The right default for backfills and nightly enrichment.
  claude-online  concurrent Messages API calls with a semaphore: for small, latency-sensitive increments

All three return the same validated structure, so the pipeline, the tables and the evals don't care.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import ClassVar

PROMPT_VERSION = "triage-v1"     # bump on any prompt or schema change: it is part of the output's key
CATEGORIES = ["outage", "capacity", "cost", "security", "access"]
SEVERITIES = ["low", "medium", "high", "critical"]

SYSTEM = f"""You triage operations tickets about an AWS EC2 fleet. For each ticket return:
category: one of {CATEGORIES}
severity: one of {SEVERITIES} (critical = production down; high = data at risk or security exposure)
instance_ids: every EC2 instance id mentioned (i-...)
summary: at most 15 words, no personal data"""

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["category", "severity", "instance_ids", "summary"],
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "severity": {"type": "string", "enum": SEVERITIES},
        "instance_ids": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}

# USD per token. Batch API is half price. Set explicitly per model: never trust a stale default.
PRICES = {"claude-opus-5-5": (4e-6, 20e-6), "claude-sonnet-5-5": (2e-6, 10e-6), "claude-haiku-4-5": (1e-6, 5e-6),
          "scripted": (0.0, 0.0)}


@dataclass
class Outcome:
    result: dict | None
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    raw: str | None = None


def validate(obj: dict) -> dict:
    """Schema-check model output even with structured outputs on: the pipeline must never trust shape."""
    if obj.get("category") not in CATEGORIES or obj.get("severity") not in SEVERITIES:
        raise ValueError(f"invalid enum in {obj}")
    ids = [i for i in obj.get("instance_ids", []) if re.fullmatch(r"i-[0-9a-f]{8,17}", i)]
    return {"category": obj["category"], "severity": obj["severity"], "instance_ids": ids,
            "summary": str(obj.get("summary", ""))[:200]}


class Scripted:
    model = "scripted"
    RULES: ClassVar[list[tuple[str, str, str]]] = [
        ("outage", "critical", r"unreachable|down|crashed|no dags"),
        ("security", "high", r"0\.0\.0\.0|imdsv1|security group"),
        ("access", "medium", r"access|iam role|ssm session"),
        ("cost", "low", r"idle|left running|stop it"),
        ("capacity", "high", r"oom|disk|no space"),
        ("capacity", "medium", r"cpu"),
    ]

    async def run(self, items: dict[str, str]) -> dict[str, Outcome]:
        out = {}
        for key, text in items.items():
            low = text.lower()
            cat, sev = next(((c, s) for c, s, p in self.RULES if re.search(p, low)), ("capacity", "medium"))
            res = {"category": cat, "severity": sev, "instance_ids": re.findall(r"i-[0-9a-f]{17}", text),
                   "summary": " ".join(text.split()[:12])}
            out[key] = Outcome(validate(res), input_tokens=len(SYSTEM + text) // 4, output_tokens=40)
        return out


def _params(model: str, text: str) -> dict:
    return {
        "model": model, "max_tokens": 1024, "system": SYSTEM,
        "messages": [{"role": "user", "content": f"<ticket>\n{text}\n</ticket>"}],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
    }


def _parse(message) -> Outcome:
    text = next((b.text for b in message.content if b.type == "text"), "")
    usage = message.usage
    try:
        return Outcome(validate(json.loads(text)), usage.input_tokens, usage.output_tokens)
    except (ValueError, KeyError) as e:
        return Outcome(None, usage.input_tokens, usage.output_tokens, error=str(e), raw=text[:2000])


class ClaudeOnline:
    def __init__(self, client, model: str = "claude-opus-5-5", concurrency: int = 16):
        self.client, self.model, self.sem = client, model, asyncio.Semaphore(concurrency)

    async def run(self, items: dict[str, str]) -> dict[str, Outcome]:
        import anthropic

        async def one(key: str, text: str) -> tuple[str, Outcome]:
            async with self.sem:
                try:
                    return key, _parse(await self.client.messages.create(**_params(self.model, text)))
                except anthropic.APIStatusError as e:
                    return key, Outcome(None, error=f"HTTP {e.status_code}: {e.message}")

        return dict(await asyncio.gather(*(one(k, t) for k, t in items.items())))


class ClaudeBatch:
    """Message Batches API: submit everything, poll, then stream results keyed by custom_id."""

    def __init__(self, client, model: str = "claude-opus-5-5", poll_s: float = 30):
        self.client, self.model, self.poll_s = client, model, poll_s

    async def run(self, items: dict[str, str]) -> dict[str, Outcome]:
        # custom_id must be unique within a batch and is the ONLY way to match results: results come back
        # in any order. Keep it short and derivable (here: the ticket id).
        batch = await self.client.messages.batches.create(
            requests=[{"custom_id": k, "params": _params(self.model, t)} for k, t in items.items()])
        while batch.processing_status != "ended":
            await asyncio.sleep(self.poll_s)
            batch = await self.client.messages.batches.retrieve(batch.id)
        out: dict[str, Outcome] = {}
        async for entry in await self.client.messages.batches.results(batch.id):
            if entry.result.type == "succeeded":
                out[entry.custom_id] = _parse(entry.result.message)
            else:
                out[entry.custom_id] = Outcome(None, error=f"batch result {entry.result.type}")
        return out


def backend(mode: str, model: str | None = None):
    if mode == "scripted":
        return Scripted()
    import anthropic
    client = anthropic.AsyncAnthropic()
    model = model or "claude-opus-5-5"
    return ClaudeBatch(client, model) if mode == "claude-batch" else ClaudeOnline(client, model)


def cost(model: str, input_tokens: int, output_tokens: int, batch: bool) -> float:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return (input_tokens * pin + output_tokens * pout) * (0.5 if batch else 1.0)
