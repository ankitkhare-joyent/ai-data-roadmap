"""A fake Anthropic Messages API, so the gateway lab runs without spending money.

It speaks just enough of POST /v1/messages for LiteLLM: text responses with realistic usage numbers.
Behaviour is driven by the model name, so the gateway config can model a flaky primary and a healthy
fallback:
    *opus*    fails with 529 overloaded on FAIL_RATE of requests (compose sets 0.8), else answers slowly
    *sonnet*  answers
    *haiku*   answers fast
Every response says which model served it, so you can see routing and fallbacks from the client.
"""
from __future__ import annotations

import asyncio
import os
import random
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="fake-anthropic")
FAIL_RATE = float(os.environ.get("FAIL_RATE", "0.5"))
LATENCY = {"opus": 0.8, "sonnet": 0.4, "haiku": 0.1}
stats: dict[str, int] = {}


def _tier(model: str) -> str:
    return next((t for t in LATENCY if t in model), "sonnet")


@app.post("/v1/messages")
async def messages(req: Request):
    body = await req.json()
    model = body.get("model", "unknown")
    tier = _tier(model)
    stats[f"{tier}_requests"] = stats.get(f"{tier}_requests", 0) + 1
    if tier == "opus" and random.random() < FAIL_RATE:
        stats["opus_overloaded"] = stats.get("opus_overloaded", 0) + 1
        return JSONResponse({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
                            status_code=529)
    await asyncio.sleep(LATENCY[tier])
    prompt_chars = sum(len(str(m.get("content", ""))) for m in body.get("messages", []))
    text = f"[served by {model}] This is a canned answer."
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": max(1, prompt_chars // 4) + 200, "output_tokens": 400,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }


@app.get("/stats")
async def get_stats():
    return stats


@app.get("/health")
async def health():
    return {"ok": True}
