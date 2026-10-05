"""Start both specialist agents locally, then run the orchestrator against them. No Docker, no API key.

    python -m multi_agent.demo [--claude]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import secrets
import socket

import httpx
import uvicorn

from multi_agent.orchestrator import ClaudePlanner, Orchestrator
from multi_agent.specialists import build_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def specialists():
    """Run fleet-analyst and runbook-expert on local ports with fresh random tokens.

    The tokens stand in for per-agent, short-lived JWTs from OpenBao (lesson 3): each agent only accepts
    its own, so the orchestrator holds one credential per downstream agent.
    """
    servers, urls, tokens = [], {}, {}
    for kind, name in (("fleet", "fleet-analyst"), ("runbook", "runbook-expert")):
        port, token = free_port(), secrets.token_urlsafe(24)
        url = f"http://127.0.0.1:{port}"
        srv = uvicorn.Server(uvicorn.Config(build_app(kind, url, token), host="127.0.0.1", port=port,
                                            log_level="warning"))
        servers.append((srv, asyncio.create_task(srv.serve())))
        urls[name], tokens[name] = url, token
    while not all(s.started for s, _ in servers):
        await asyncio.sleep(0.05)
    try:
        yield urls, tokens
    finally:
        for s, task in servers:
            s.should_exit = True
            await task


async def main(use_claude: bool) -> None:
    async with specialists() as (urls, tokens):
        async with httpx.AsyncClient() as http:
            card = (await http.get(f"{urls['runbook-expert']}/.well-known/agent-card.json")).json()
            print("== discovery: runbook-expert's Agent Card (public)")
            print(f"   name={card['name']!r} skills={[s['id'] for s in card['skills']]} "
                  f"auth={list(card.get('securitySchemes', {}))} endpoint={card['supportedInterfaces'][0]['url']}")
            r = await http.post(f"{urls['runbook-expert']}/a2a/jsonrpc", json={"jsonrpc": "2.0", "id": 1})
            print(f"   calling it without a token: HTTP {r.status_code}")

        orch = Orchestrator(urls, tokens, planner=ClaudePlanner() if use_claude else None)
        q = "Which instance is hot right now, and what should I do to fix it?"
        print(f"\n== question: {q}")
        res = await orch.answer(q)
        for s in res.spans:
            print(f"   span {s.agent:<15} {s.state:<26} {s.seconds:5.2f}s  task={s.task_id[:8] if s.task_id else '-'}")
        print(f"   trace {res.traceparent}  delegations used: {res.delegations}")
        print("\n== answer\n" + res.answer)

        seq = await orch.answer(q, parallel=False)
        print(f"\n== latency: parallel fan-out {res.seconds:.2f}s vs sequential {seq.seconds:.2f}s")

        print("\n== a question no specialist covers")
        print("   " + (await orch.answer("Draft a haiku about Kafka.")).answer)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", action="store_true", help="let Claude choose agents (needs ANTHROPIC_API_KEY)")
    asyncio.run(main(ap.parse_args().claude))
