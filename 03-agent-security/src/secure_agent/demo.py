"""Mint OpenBao identity JWTs for three workloads and call the fleet-ops MCP server with each.

    docker compose run --rm demo
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx2
import jwt
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from secure_agent.auth import openbao_token

BAO = os.environ.get("BAO_ADDR", "http://openbao:8200")
MCP = os.environ.get("MCP_URL", "http://mcp:8300/mcp")


def creds(role: str) -> tuple[str, str]:
    d = Path("/bootstrap/approle") / role
    return (d / "role_id").read_text().strip(), (d / "secret_id").read_text().strip()


async def call(bearer: str, tool: str, args: dict) -> str:
    try:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {bearer}"}, timeout=15) as http, \
                streamable_http_client(MCP, http_client=http) as (r, w, *_), ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            text = res.content[0].text.replace("\n", " ")
            return ("ERROR " if res.is_error else "ok    ") + text[:110]
    except Exception as e:  # noqa: BLE001  (a 401 surfaces as some transport error in the client)
        return f"REFUSED {type(e).__name__}: {str(e)[:90]}"


async def main() -> None:
    tokens = {r: await openbao_token(BAO, *creds(r), r) for r in ("triage-agent", "ops-agent", "other-service")}
    print("== what a token carries (triage-agent)")
    claims = jwt.decode(tokens["triage-agent"], options={"verify_signature": False})
    print(json.dumps({k: claims[k] for k in ("iss", "aud", "sub", "scope", "agent", "exp")}, indent=2))

    print("\n== JWKS is public; the MCP server needs no secret to verify tokens")
    print(httpx2.get(f"{BAO}/v1/identity/oidc/.well-known/keys").json()["keys"][0]["kid"])

    cases = [
        ("triage-agent", "search_tickets", {"query": "OPS-4412"}),
        ("triage-agent", "stop_instance", {"instance_id": "i-0a1b2c3d4e5f60001", "reason": "idle"}),
        ("ops-agent", "stop_instance", {"instance_id": "i-0a1b2c3d4e5f60002", "reason": "idle"}),
        ("ops-agent", "stop_instance", {"instance_id": "i-0a1b2c3d4e5f60001", "reason": "idle 21 days"}),
        ("other-service", "search_tickets", {"query": "OPS"}),
        ("no token", "search_tickets", {"query": "OPS"}),
    ]
    print("\n== calls")
    for who, tool, args in cases:
        bearer = tokens.get(who, "")
        print(f"{who:<14} {tool:<16} {await call(bearer, tool, args)}")


if __name__ == "__main__":
    asyncio.run(main())
