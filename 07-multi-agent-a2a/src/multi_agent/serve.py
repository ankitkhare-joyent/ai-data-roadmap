"""Run both specialist agents on fixed local ports until Ctrl-C, for poking at them with curl (lab 07).

    python -m multi_agent.serve        # fleet-analyst on :49701, runbook-expert on :49702

Each agent gets a fresh random bearer token, written to .lab-tokens (git-ignored) as shell assignments, so
`source .lab-tokens` gives you $FLEET_TOKEN and $RUNBOOK_TOKEN. In production these are short-lived JWTs.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path

import uvicorn

from multi_agent.specialists import build_app

AGENTS = (("fleet", "FLEET", 49701), ("runbook", "RUNBOOK", 49702))


async def main() -> None:
    tokens, servers = {}, []
    for kind, var, port in AGENTS:
        tokens[var] = secrets.token_urlsafe(24)
        app = build_app(kind, f"http://127.0.0.1:{port}", tokens[var])
        servers.append(uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")))
    path = Path(".lab-tokens")
    path.write_text("".join(f"export {v}_TOKEN={t}\n" for v, t in tokens.items()))
    os.chmod(path, 0o600)
    print("fleet-analyst  http://127.0.0.1:49701\nrunbook-expert http://127.0.0.1:49702\n"
          "tokens in .lab-tokens (source it). Ctrl-C to stop.", flush=True)
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
