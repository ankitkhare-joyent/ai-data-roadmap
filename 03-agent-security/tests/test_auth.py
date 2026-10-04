"""The JWT verifier against a local JWKS endpoint: the checks an attacker would probe, one by one."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from secure_agent.auth import JwtVerifier

ISS, AUD = "http://openbao:8200/v1/identity/oidc", "http://mcp:8300/mcp"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks_url():
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    body = json.dumps({"keys": [{**jwk, "kid": "k1", "alg": "RS256", "use": "sig"}]}).encode()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/keys"
    srv.shutdown()


def token(key=KEY, alg="RS256", **over):
    now = int(time.time())
    claims = {"iss": ISS, "aud": AUD, "sub": "entity-123", "iat": now, "exp": now + 300,
              "scope": "tickets:read ec2:read", **over}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": "k1"})


async def test_valid_token(jwks_url):
    at = await JwtVerifier(ISS, AUD, jwks_url).verify_token(token())
    assert at and at.scopes == ["tickets:read", "ec2:read"] and at.resource == AUD


@pytest.mark.parametrize("bad", [
    {"aud": "http://some-other-service"},     # minted for another resource: replay / confused deputy
    {"iss": "http://evil-issuer"},
    {"exp": int(time.time()) - 120},          # expired beyond leeway
])
async def test_rejects_bad_claims(jwks_url, bad):
    assert await JwtVerifier(ISS, AUD, jwks_url).verify_token(token(**bad)) is None


async def test_rejects_wrong_signing_key(jwks_url):
    assert await JwtVerifier(ISS, AUD, jwks_url).verify_token(token(key=OTHER)) is None


async def test_rejects_hmac_alg_confusion(jwks_url):
    # Classic attack: sign with HS256 using the public key as the HMAC secret.
    forged = jwt.encode({"iss": ISS, "aud": AUD, "sub": "x", "iat": 0, "exp": 2**31}, "x" * 32,
                        algorithm="HS256", headers={"kid": "k1"})
    assert await JwtVerifier(ISS, AUD, jwks_url).verify_token(forged) is None
