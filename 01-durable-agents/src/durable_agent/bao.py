"""A minimal async OpenBao client: AppRole login and KV v2 reads, against the plain HTTP API.

Same contract as the client in rag-vector-srch, trimmed to what this lesson needs. The role_id and
secret_id arrive as files mounted into exactly one container; production replaces them with platform
identity (Kubernetes or AWS IAM auth), and the code below changes only in the login call.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("durable_agent.bao")


class BaoError(RuntimeError):
    pass


class BaoClient:
    def __init__(self, addr: str | None = None, approle_dir: str | None = None, kv_mount: str = "secret"):
        self._http = httpx.AsyncClient(base_url=addr or os.environ.get("BAO_ADDR", "http://openbao:8200"),
                                       timeout=10)
        self._dir = Path(approle_dir or os.environ.get("BAO_APPROLE_DIR", "/run/secrets/bao"))
        self._mount = kv_mount
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def _token_value(self) -> str:
        async with self._lock:
            if self._token and self._expires - time.monotonic() > 60:
                return self._token
            try:
                role_id = (self._dir / "role_id").read_text().strip()
                secret_id = (self._dir / "secret_id").read_text().strip()
            except FileNotFoundError as e:
                raise BaoError(f"AppRole credentials missing in {self._dir}; run bao-bootstrap first") from e
            r = await self._http.post("/v1/auth/approle/login", json={"role_id": role_id, "secret_id": secret_id})
            self._check(r, "AppRole login")
            auth = r.json()["auth"]
            self._token, self._expires = auth["client_token"], time.monotonic() + auth["lease_duration"]
            log.info("OpenBao login ok, policies=%s ttl=%ss", auth["token_policies"], auth["lease_duration"])
            return self._token

    async def kv_get(self, path: str, version: int | None = None) -> tuple[dict, int]:
        """Return (data, version) of a KV v2 secret; a specific version when asked."""
        params = {"version": version} if version else None
        r = await self._http.get(f"/v1/{self._mount}/data/{path}", params=params,
                                 headers={"X-Vault-Token": await self._token_value()})
        self._check(r, f"read {path}")
        body = r.json()["data"]
        return body["data"], body["metadata"]["version"]

    @staticmethod
    def _check(r: httpx.Response, what: str) -> None:
        if r.status_code >= 400:
            raise BaoError(f"{what} failed: HTTP {r.status_code} {r.text[:300]}")
