"""End to end over Streamable HTTP: a real MCP client, the real server, tokens from a local issuer."""
import socket
import threading
import time

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from secure_agent.auth import JwtVerifier
from secure_agent.mcp_server import build_server
from tests.test_auth import AUD, ISS, jwks_url, token  # noqa: F401  (fixture reuse)


@pytest.fixture(scope="module")
def mcp_url(jwks_url):  # noqa: F811
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = build_server(JwtVerifier(ISS, AUD, jwks_url), resource=AUD).streamable_http_app(host="127.0.0.1")
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True


async def call(url, bearer, tool, args):
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    async with httpx2.AsyncClient(headers=headers, timeout=10) as http, \
            streamable_http_client(url, http_client=http) as (r, w, *_), ClientSession(r, w) as s:
        await s.initialize()
        return await s.call_tool(tool, args)


async def test_no_token_gets_401_with_resource_metadata(mcp_url):
    async with httpx2.AsyncClient() as http:
        r = await http.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


async def test_read_scope_can_read_but_not_stop(mcp_url):
    t = token(scope="tickets:read ec2:read")
    res = await call(mcp_url, t, "search_tickets", {"query": "OPS-4412"})
    text = res.content[0].text
    assert not res.is_error and "<untrusted-" in text and "[REDACTED:EMAIL]" in text
    res = await call(mcp_url, t, "stop_instance", {"instance_id": "i-0a1b2c3d4e5f60001", "reason": "x"})
    assert res.is_error and "ec2:write" in res.content[0].text


async def test_write_scope_still_cannot_stop_prod(mcp_url):
    t = token(scope="tickets:read ec2:read ec2:write")
    res = await call(mcp_url, t, "stop_instance", {"instance_id": "i-0a1b2c3d4e5f60002", "reason": "x"})
    assert res.is_error and "prod" in res.content[0].text
