"""Configure OpenBao for this lesson. Idempotent: safe to re-run.

    1. init       first run only; root token + recovery key kept in a volume (LOCAL ONLY)
    2. engines    kv-v2 at secret/, approle auth
    3. secrets    durable-agent/anthropic from ANTHROPIC_API_KEY (if set),
                  durable-agent/codec: a random AES-256 key, created once (rotate: --rotate-codec)
    4. policies   agent-worker, agent-cli
    5. approles   one per service; role_id + secret_id written to /bootstrap/approle/<service>/

Production: apply this with OpenTofu's Vault provider, revoke the root token, and authenticate workloads
with Kubernetes or AWS IAM auth instead of secret_id files.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx

log = logging.getLogger("bao-bootstrap")

BAO_ADDR = os.environ.get("BAO_ADDR", "http://openbao:8200")
INIT_FILE = Path("/bootstrap/init/init.json")
APPROLE_DIR = Path("/bootstrap/approle")
POLICY_DIR = Path(os.environ.get("BAO_POLICY_DIR", "/app/openbao/policies"))
APPROLES = {"agent-worker": "1h", "agent-cli": "15m"}


class Bao:
    def __init__(self) -> None:
        self.http = httpx.Client(base_url=BAO_ADDR, timeout=15)
        self.token: str | None = None

    def call(self, method: str, path: str, body: dict | None = None, ok=(200, 204)) -> dict:
        r = self.http.request(method, f"/v1/{path}", json=body,
                              headers={"X-Vault-Token": self.token} if self.token else {})
        if r.status_code not in ok:
            sys.exit(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}


def initialise(bao: Bao) -> None:
    for _ in range(60):
        try:
            health = bao.http.get("/v1/sys/health").json()
            break
        except (httpx.TransportError, ValueError):
            time.sleep(1)
    else:
        sys.exit(f"OpenBao not reachable at {BAO_ADDR}")
    if not health["initialized"]:
        out = bao.call("PUT", "sys/init", {"recovery_shares": 1, "recovery_threshold": 1})
        INIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        INIT_FILE.write_text(json.dumps(out))
        INIT_FILE.chmod(0o600)
        log.info("initialised OpenBao (auto-unseal); root token kept in the bao-init volume, LOCAL ONLY")
    for _ in range(30):
        if not bao.call("GET", "sys/seal-status")["sealed"]:
            break
        time.sleep(1)
    else:
        sys.exit("OpenBao is still sealed")
    bao.token = json.loads(INIT_FILE.read_text())["root_token"]


def kv_write_if_changed(bao: Bao, path: str, data: dict, only_if_absent: bool = False) -> None:
    cur = bao.call("GET", f"secret/data/{path}", ok=(200, 404)).get("data") or {}
    version = (cur.get("metadata") or {}).get("version", 0)
    if cur.get("data") == data or (only_if_absent and cur.get("data")):
        log.info("secret/%s unchanged (version %s)", path, version)
        return
    out = bao.call("POST", f"secret/data/{path}", {"options": {"cas": version}, "data": data})
    log.info("wrote secret/%s version %s", path, out["data"]["version"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    rotate = "--rotate-codec" in sys.argv
    bao = Bao()
    initialise(bao)

    if "secret/" not in bao.call("GET", "sys/mounts")["data"]:
        bao.call("POST", "sys/mounts/secret", {"type": "kv", "options": {"version": "2"}})
    if "approle/" not in bao.call("GET", "sys/auth")["data"]:
        bao.call("POST", "sys/auth/approle", {"type": "approle"})

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        kv_write_if_changed(bao, "durable-agent/anthropic", {"api_key": key})
    else:
        log.warning("ANTHROPIC_API_KEY not set: fine for LLM_MODE=scripted; set it in .env for the real model")
    new_key = {"key": base64.b64encode(os.urandom(32)).decode(), "alg": "AES-256-GCM"}
    kv_write_if_changed(bao, "durable-agent/codec", new_key, only_if_absent=not rotate)

    for f in sorted(POLICY_DIR.glob("*.hcl")):
        bao.call("PUT", f"sys/policies/acl/{f.stem}", {"policy": f.read_text()})
    for name, ttl in APPROLES.items():
        bao.call("POST", f"auth/approle/role/{name}", {
            "token_policies": [name], "token_ttl": ttl, "token_max_ttl": "24h",
            "secret_id_ttl": "720h", "token_type": "service"})
        role_id = bao.call("GET", f"auth/approle/role/{name}/role-id")["data"]["role_id"]
        secret_id = bao.call("POST", f"auth/approle/role/{name}/secret-id")["data"]["secret_id"]
        d = APPROLE_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        for fname, value in (("role_id", role_id), ("secret_id", secret_id)):
            p = d / fname
            p.unlink(missing_ok=True)
            p.write_text(value)
            p.chmod(0o444)
    log.info("policies and approles ready: %s", ", ".join(APPROLES))


if __name__ == "__main__":
    main()
