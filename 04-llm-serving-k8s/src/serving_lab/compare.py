"""Run the same agentic workload through each routing strategy and print one line per strategy.

    python -m serving_lab.compare --url http://router:8080
"""
from __future__ import annotations

import argparse
import asyncio
import time

import httpx

from serving_lab.bench import Workload, run_sessions, summarise


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:49480")
    ap.add_argument("--model", default="fleet-8b")
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--sessions", type=int, default=48)
    ap.add_argument("--tenants", type=int, default=12)
    a = ap.parse_args()
    print(f"{a.sessions} sessions x {Workload().turns} turns, {a.concurrency} in flight, "
          f"{a.tenants} tenants with {Workload().system_tokens}-token system prompts\n")
    for k, strategy in enumerate(("round_robin", "least_loaded", "prefix_aware")):
        # Same shape of work for each strategy, but fresh prompts (a new seed), so every strategy starts with
        # cold caches and none benefits from what the previous run left in the simulators' KV caches.
        wl = Workload(tenants=a.tenants, seed=11 + k)
        t = time.perf_counter()
        res = await run_sessions(a.url, a.model, wl, a.concurrency, a.sessions, headers={"x-route": strategy})
        print(summarise(strategy, res, time.perf_counter() - t))
        await asyncio.sleep(1)
    async with httpx.AsyncClient() as c:
        print("\npicks per backend:", (await c.get(f"{a.url}/stats")).json()["picks"])


if __name__ == "__main__":
    asyncio.run(main())
