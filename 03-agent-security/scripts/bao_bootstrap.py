"""Configure OpenBao as the token issuer for the fleet-ops MCP server. Standard library only; idempotent.

    identity/oidc/config         issuer = http://openbao:8200  ->  iss = http://openbao:8200/v1/identity/oidc
    identity/oidc/key/fleet-ops  RS256 signing key, rotated daily; public half served as JWKS (no auth)
    identity/oidc/role/<agent>   aud (client_id) = the MCP server URL, ttl 5m, scope claim fixed per role
    auth/approle/role/<agent>    the workload's login; its policy can mint only its own role's tokens

Each agent: AppRole login -> GET identity/oidc/token/<role> -> JWT -> Authorization: Bearer to MCP.
In Kubernetes, swap AppRole for OpenBao's Kubernetes auth and nothing else changes.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ADDR = os.environ.get("BAO_ADDR", "http://openbao:8200")
MCP = os.environ.get("MCP_RESOURCE", "http://mcp:8300/mcp")
INIT = Path("/bootstrap/init/init.json")
TOKEN = None
ROLES = {
    # role            audience                          scope claim
    "triage-agent": (MCP, "tickets:read ec2:read"),
    "ops-agent": (MCP, "tickets:read ec2:read ec2:write"),
    "other-service": ("http://billing:9000/api", "tickets:read ec2:read ec2:write"),
}


def call(method, path, body=None, ok=(200, 204)):
    req = urllib.request.Request(f"{ADDR}/v1/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Vault-Token": TOKEN} if TOKEN else {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code not in ok:
            sys.exit(f"{method} {path} -> {e.code}: {e.read()[:300]!r}")
        raw = e.read()
    return json.loads(raw) if raw else {}


for _ in range(60):
    try:
        health = call("GET", "sys/health", ok=(200, 429, 501, 503))
        break
    except (urllib.error.URLError, ConnectionError):
        time.sleep(1)
if not health.get("initialized"):
    INIT.parent.mkdir(parents=True, exist_ok=True)
    INIT.write_text(json.dumps(call("PUT", "sys/init", {"recovery_shares": 1, "recovery_threshold": 1})))
    INIT.chmod(0o600)
while call("GET", "sys/seal-status")["sealed"]:
    time.sleep(1)
# Unsealed is not ready: raft still has to elect this node leader. Wait for "active" (HTTP 200).
for _ in range(60):
    try:
        with urllib.request.urlopen(f"{ADDR}/v1/sys/health", timeout=5) as r:
            if r.status == 200:
                break
    except urllib.error.URLError:
        pass
    time.sleep(1)
TOKEN = json.loads(INIT.read_text())["root_token"]

if "approle/" not in call("GET", "sys/auth")["data"]:
    call("POST", "sys/auth/approle", {"type": "approle"})
call("POST", "identity/oidc/config", {"issuer": ADDR})
call("POST", "identity/oidc/key/fleet-ops", {"algorithm": "RS256", "rotation_period": "24h",
                                             "verification_ttl": "24h", "allowed_client_ids": ["*"]})
for role, (aud, scope) in ROLES.items():
    template = json.dumps({"scope": scope}).rstrip("}") + ', "agent": {{identity.entity.name}}}'
    call("POST", f"identity/oidc/role/{role}", {"key": "fleet-ops", "client_id": aud, "ttl": "5m",
                                                "template": template})
    call("PUT", f"sys/policies/acl/{role}", {"policy": Path(f"/policies/{role}.hcl").read_text()})
    call("POST", f"auth/approle/role/{role}", {"token_policies": [role], "token_ttl": "15m",
                                               "token_max_ttl": "1h", "secret_id_ttl": "720h"})
    ids = {"role_id": call("GET", f"auth/approle/role/{role}/role-id")["data"]["role_id"],
           "secret_id": call("POST", f"auth/approle/role/{role}/secret-id")["data"]["secret_id"]}
    for name, value in ids.items():
        p = Path("/bootstrap/approle") / role / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.unlink(missing_ok=True)
        p.write_text(value)
        p.chmod(0o444)
    print(f"role {role}: aud={aud} scope={scope!r}")
print("OpenBao is issuing identity tokens; JWKS at", f"{ADDR}/v1/identity/oidc/.well-known/keys")
