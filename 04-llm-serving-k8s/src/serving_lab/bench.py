"""Load generator for any OpenAI-compatible endpoint (vLLM, the llm-d simulator, KServe, Ray Serve LLM).

    python -m serving_lab.bench --url http://localhost:49480 --concurrency 1 8 32

It replays an *agentic* workload, because that is what your platform will actually see: a few tenants,
each with a long, stable system prompt (tools, instructions, schemas), and sessions that grow turn by turn.
That shape is exactly where prefix caching and cache-aware routing pay off.

Metrics, the ones serving teams put SLOs on:
  TTFT   time to first token: queueing + prefill. What users feel as "it started answering".
  ITL    inter-token latency (a.k.a. TPOT): decode speed per stream.
  E2E    whole request.
  tok/s  output tokens per second across all streams: what you pay for.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class Result:
    ttft: float
    itls: list[float]
    e2e: float
    out_tokens: int
    ok: bool = True


@dataclass
class Workload:
    tenants: int = 6
    system_tokens: int = 1500      # tools + instructions: identical for every turn of a tenant
    turns: int = 4                 # each turn appends to the session history
    user_tokens: int = 120
    max_tokens: int = 64
    seed: int = 7
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self):
        self._rng = random.Random(self.seed)

    def _words(self, n: int, tag: str) -> str:
        # ~1 token per short word for simulated tokenisers; real tokenisers differ, the shape is what matters
        return " ".join(f"{tag}{self._rng.randint(0, 999)}" for _ in range(n))

    def sessions(self, n: int) -> list[list[list[dict]]]:
        """n sessions, each a list of turn requests (message lists), sharing per-tenant system prompts."""
        systems = [self._words(self.system_tokens, f"t{t}w") for t in range(self.tenants)]
        out = []
        for s in range(n):
            history = [{"role": "system", "content": systems[s % self.tenants]}]
            turns = []
            for _ in range(self.turns):
                history = history + [{"role": "user", "content": self._words(self.user_tokens, "u")}]
                turns.append(list(history))
                history = history + [{"role": "assistant", "content": self._words(self.max_tokens, "a")}]
            out.append(turns)
        return out


async def one(client: httpx.AsyncClient, url: str, model: str, messages: list[dict], max_tokens: int,
              headers: dict | None = None) -> Result:
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True,
            "stream_options": {"include_usage": True}}
    t0 = time.perf_counter()
    first = last = None
    itls: list[float] = []
    tokens = 0
    try:
        async with client.stream("POST", f"{url}/v1/chat/completions", json=body, headers=headers) as r:
            if r.status_code != 200:
                await r.aread()
                return Result(0, [], time.perf_counter() - t0, 0, ok=False)
            async for line in r.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                chunk = json.loads(line[6:])
                if chunk.get("usage"):
                    tokens = chunk["usage"].get("completion_tokens", tokens)
                choices = chunk.get("choices") or []
                if choices and choices[0].get("delta", {}).get("content"):
                    now = time.perf_counter()
                    if first is None:
                        first = now
                    else:
                        itls.append(now - last)
                    last = now
    except httpx.HTTPError:
        return Result(0, [], time.perf_counter() - t0, 0, ok=False)
    end = time.perf_counter()
    return Result((first or end) - t0, itls, end - t0, tokens or len(itls) + 1)  # usage when sent, else chunks


async def run_sessions(url: str, model: str, wl: Workload, concurrency: int, n_sessions: int,
                       headers: dict | None = None) -> list[Result]:
    """Run sessions with `concurrency` in flight; turns within a session are sequential, like a real agent."""
    sem = asyncio.Semaphore(concurrency)
    results: list[Result] = []
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency * 2)
    async with httpx.AsyncClient(timeout=120, limits=limits) as client:
        async def session(turns):
            async with sem:
                for msgs in turns:
                    results.append(await one(client, url, model, msgs, wl.max_tokens, headers))
        await asyncio.gather(*(session(t) for t in wl.sessions(n_sessions)))
    return results


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def summarise(label: str, results: list[Result], wall: float) -> str:
    ok = [r for r in results if r.ok]
    ttft = [r.ttft * 1000 for r in ok]
    itl = [x * 1000 for r in ok for x in r.itls]
    toks = sum(r.out_tokens for r in ok)
    return (f"{label:<28} n={len(results):4d} err={len(results) - len(ok):3d} | "
            f"TTFT p50 {pct(ttft, 50):7.0f} p95 {pct(ttft, 95):7.0f} ms | "
            f"ITL p50 {statistics.median(itl) if itl else float('nan'):5.1f} ms | "
            f"{toks / wall:7.0f} out tok/s")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:49480")
    ap.add_argument("--model", default="acme/fleet-8b")
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1, 8, 24])
    ap.add_argument("--sessions", type=int, default=24)
    a = ap.parse_args()
    wl = Workload()
    for c in a.concurrency:
        t = time.perf_counter()
        res = await run_sessions(a.url, a.model, wl, c, a.sessions)
        print(summarise(f"concurrency {c}", res, time.perf_counter() - t))


if __name__ == "__main__":
    asyncio.run(main())
