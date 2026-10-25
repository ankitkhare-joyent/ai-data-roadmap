"""A teaching-sized inference-aware router: what the Gateway API Inference Extension's endpoint picker does.

    python -m serving_lab.router     # :8080, backends from BACKENDS="http://sim-1:8000,http://sim-2:8000,..."

A Kubernetes Service balances connections round-robin. For LLMs that is the wrong signal twice over:
  - requests differ wildly in cost (a 50-token question vs a 30k-token agent turn), so equal counts are not
    equal load: look at each replica's queue and KV-cache pressure instead;
  - each replica keeps a prefix cache of recently seen prompts. Sending a tenant's next turn to the
    replica that already holds its 1,500-token system prompt skips that prefill entirely. Round-robin
    scatters a session across replicas and pays the prefill again on each.

Strategies, picked per request with the `x-route` header (so one bench run can compare them):
  round_robin   what a Service does
  least_loaded  fewest running + waiting requests (from vLLM's /metrics, refreshed every 250 ms,
                plus requests this router has in flight)
  prefix_aware  rendezvous-hash the prompt prefix to a preferred replica, unless it is noticeably busier
                than the least-loaded one; then spill over. llm-d's EPP does the same with real
                block-level prefix indexes and more scorers (see k8s/production/kserve-llmisvc.yaml).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import logging
import os
import re
from collections import Counter

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

log = logging.getLogger("router")
BACKENDS = [b.strip() for b in os.environ.get("BACKENDS", "http://localhost:8000").split(",") if b.strip()]
SPILL_SLACK = int(os.environ.get("SPILL_SLACK", "3"))   # how much busier the preferred replica may be
PREFIX_CHARS = int(os.environ.get("PREFIX_CHARS", "4000"))
METRIC = re.compile(r"^vllm:(num_requests_running|num_requests_waiting)\{[^}]*\}\s+([0-9.eE+-]+)", re.MULTILINE)


class Pool:
    def __init__(self, backends: list[str]):
        self.backends = backends
        self.scraped: dict[str, float] = dict.fromkeys(backends, 0.0)
        self.inflight: Counter[str] = Counter()
        self.picks: Counter[tuple[str, str]] = Counter()
        self._rr = itertools.cycle(backends)

    def load(self, b: str) -> float:
        # Scraped queue lags by up to one interval; local in-flight count reacts instantly.
        return max(self.scraped[b], self.inflight[b])

    def pick(self, strategy: str, prefix: str) -> str:
        if strategy == "round_robin":
            return next(self._rr)
        least = min(self.backends, key=self.load)
        if strategy == "least_loaded":
            return least
        preferred = max(self.backends, key=lambda b: hashlib.sha256(f"{b}|{prefix}".encode()).digest())
        return preferred if self.load(preferred) <= self.load(least) + SPILL_SLACK else least

    async def scrape_forever(self, client: httpx.AsyncClient) -> None:
        while True:
            for b in self.backends:
                with contextlib.suppress(httpx.HTTPError):
                    text = (await client.get(f"{b}/metrics", timeout=1)).text
                    self.scraped[b] = sum(float(v) for _, v in METRIC.findall(text))
            await asyncio.sleep(0.25)


def prompt_prefix(body: dict) -> str:
    """The stable front of the prompt: system message first, then as much history as fits."""
    text = "".join(str(m.get("content", "")) for m in body.get("messages", []))
    return text[:PREFIX_CHARS]


pool = Pool(BACKENDS)
client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5),
                           limits=httpx.Limits(max_connections=500, max_keepalive_connections=200))


async def chat(request: Request):
    body = await request.json()
    strategy = request.headers.get("x-route", os.environ.get("DEFAULT_ROUTE", "prefix_aware"))
    backend = pool.pick(strategy, prompt_prefix(body))
    pool.picks[(strategy, backend)] += 1
    pool.inflight[backend] += 1
    try:
        upstream = await client.send(client.build_request("POST", f"{backend}/v1/chat/completions", json=body),
                                     stream=True)
    except httpx.HTTPError as e:
        # No health checks here (an EPP has them): a dead replica is only noticed when a request fails.
        pool.inflight[backend] -= 1
        return JSONResponse({"error": f"backend {backend} unavailable: {type(e).__name__}"}, status_code=502,
                            headers={"x-backend": backend})

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            pool.inflight[backend] -= 1

    return StreamingResponse(relay(), status_code=upstream.status_code,
                             media_type=upstream.headers.get("content-type"),
                             headers={"x-backend": backend})


async def stats(_: Request):
    by = {f"{s} -> {b.split('//')[-1]}": n for (s, b), n in sorted(pool.picks.items())}
    return JSONResponse({"picks": by, "load": {b: pool.load(b) for b in BACKENDS}})


async def health(_: Request):
    return JSONResponse({"ok": True, "backends": BACKENDS})


@contextlib.asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(pool.scrape_forever(client))
    yield
    task.cancel()
    await client.aclose()


app = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"]),
                        Route("/stats", stats), Route("/health", health)], lifespan=lifespan)

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
