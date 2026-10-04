"""The MCP server as an OAuth 2.1 resource server, trusting JWTs that OpenBao issues.

MCP's authorization spec (hardened again in the 2026-07-28 revision) makes an HTTP MCP server a plain
OAuth resource server. The checks below are the ones that matter, in the order an attacker would try them:

  signature   RS256/ES256 against the issuer's JWKS; reject "none" and HMAC algorithms outright
  iss         exactly our authorization server (no "any issuer that signs")
  aud         exactly THIS server's resource URL (RFC 8707). Without it, a token minted for another
              service is replayable here: the confused-deputy problem the spec calls "token passthrough"
  exp/nbf     with small leeway; tokens are minutes long, never days
  scope       per tool, checked inside the tool at call time

The issuer here is OpenBao's identity OIDC provider: an agent logs in to OpenBao with its workload
identity (AppRole locally, Kubernetes auth in a cluster) and asks for a token for one role. Each role has
a fixed audience (the MCP server URL) and a fixed scope claim, so the agent cannot ask for more.
In a user-facing deployment the issuer is your IdP (Okta, Entra, Keycloak) running the full
authorization-code + PKCE flow; this verifier does not change.
"""
from __future__ import annotations

import logging
import time

import httpx
import jwt
from mcp.server.auth.provider import AccessToken

log = logging.getLogger("secure_agent.auth")
ALLOWED_ALGS = ["RS256", "ES256", "EdDSA"]


class JwtVerifier:
    def __init__(self, issuer: str, audience: str, jwks_url: str, leeway_s: int = 30, jwks_ttl_s: int = 300):
        self.issuer, self.audience, self.leeway = issuer, audience, leeway_s
        self._jwks = jwt.PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=jwks_ttl_s)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=ALLOWED_ALGS, audience=self.audience,
                                issuer=self.issuer, leeway=self.leeway,
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except (jwt.PyJWTError, httpx.HTTPError) as e:
            log.warning("rejected token: %s", e)       # never log the token itself
            return None
        scopes = str(claims.get("scope", "")).split()
        return AccessToken(token=token, client_id=str(claims.get("client_id") or claims["sub"]), scopes=scopes,
                           expires_at=int(claims["exp"]), resource=self.audience, subject=claims["sub"],
                           claims={k: v for k, v in claims.items() if k in ("iss", "agent", "namespace")})


async def openbao_token(bao_addr: str, role_id: str, secret_id: str, oidc_role: str) -> str:
    """Workload login to OpenBao, then mint a short-lived identity JWT for one OIDC role."""
    async with httpx.AsyncClient(base_url=bao_addr, timeout=10) as http:
        r = await http.post("/v1/auth/approle/login", json={"role_id": role_id, "secret_id": secret_id})
        r.raise_for_status()
        bao_token = r.json()["auth"]["client_token"]
        r = await http.get(f"/v1/identity/oidc/token/{oidc_role}", headers={"X-Vault-Token": bao_token})
        r.raise_for_status()
        data = r.json()["data"]
        log.info("minted %s token, ttl %ss", oidc_role, data["ttl"])
        return data["token"]


def seconds_left(token: str) -> float:
    return jwt.decode(token, options={"verify_signature": False})["exp"] - time.time()
