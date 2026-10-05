"""Configure OpenBao for the gateway lab. Standard library only; idempotent.

    1. init        auto-unseal; root token kept in the bao-init volume (LOCAL ONLY)
    2. engines     kv-v2 at secret/, database at database/, approle
    3. secrets     secret/litellm/ANTHROPIC_API_KEY  {"key": ...}  (real key from .env, or a placeholder
                   the fake provider ignores)
                   secret/gateway/runtime            master_key, salt_key: generated once
    4. database    Postgres connection + static role "litellm": OpenBao owns that user's password
    5. policy + approle "litellm"; role_id/secret_id into /bootstrap/approle/litellm/
    6. admin key   the master key is written to /bootstrap/admin/master_key for the demo client only
"""
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ADDR = os.environ.get("BAO_ADDR", "http://openbao:8200")
INIT = Path("/bootstrap/init/init.json")
TOKEN = None


def call(method, path, body=None, ok=(200, 204)):
    req = urllib.request.Request(f"{ADDR}/v1/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Vault-Token": TOKEN} if TOKEN else {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code in ok:
            raw = e.read()
            return json.loads(raw) if raw else {}
        sys.exit(f"{method} {path} -> {e.code}: {e.read()[:300]!r}")


def kv(path):
    return (call("GET", f"secret/data/{path}", ok=(200, 404)).get("data") or {})


def kv_put(path, data):
    cur = kv(path)
    if cur.get("data") == data:
        return print(f"secret/{path} unchanged")
    call("POST", f"secret/data/{path}", {"options": {"cas": (cur.get("metadata") or {}).get("version", 0)},
                                         "data": data})
    print(f"wrote secret/{path}")


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
    print("initialised OpenBao")
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

mounts = call("GET", "sys/mounts")["data"]
if "secret/" not in mounts:
    call("POST", "sys/mounts/secret", {"type": "kv", "options": {"version": "2"}})
if "database/" not in mounts:
    call("POST", "sys/mounts/database", {"type": "database"})
if "approle/" not in call("GET", "sys/auth")["data"]:
    call("POST", "sys/auth/approle", {"type": "approle"})

real_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
current = kv("litellm/ANTHROPIC_API_KEY").get("data")
if real_key:
    kv_put("litellm/ANTHROPIC_API_KEY", {"key": real_key})
elif not current:
    kv_put("litellm/ANTHROPIC_API_KEY", {"key": "placeholder-for-the-fake-provider"})
if not kv("gateway/runtime").get("data"):
    kv_put("gateway/runtime", {"master_key": "sk-" + secrets.token_urlsafe(32),
                               "salt_key": "sk-" + secrets.token_urlsafe(32)})

if not call("GET", "database/config/gateway-pg", ok=(200, 404)).get("data"):
    call("POST", "database/config/gateway-pg", {
        "plugin_name": "postgresql-database-plugin",
        "connection_url": "postgresql://{{username}}:{{password}}@postgres:5432/litellm?sslmode=disable",
        "username": "postgres", "password": os.environ["PG_BOOTSTRAP_PASSWORD"],
        "allowed_roles": ["litellm"], "password_authentication": "scram-sha-256"})
    call("POST", "database/rotate-root/gateway-pg")   # now only OpenBao knows the superuser password
    print("configured database/config/gateway-pg and rotated the superuser password")
# Static role: the username stays "litellm", OpenBao sets and rotates its password.
call("POST", "database/static-roles/litellm", {"db_name": "gateway-pg", "username": "litellm",
                                               "rotation_period": "720h"})

call("PUT", "sys/policies/acl/litellm", {"policy": Path("/policies/litellm.hcl").read_text()})
call("POST", "auth/approle/role/litellm", {"token_policies": ["litellm"], "token_ttl": "1h",
                                           "token_max_ttl": "24h", "secret_id_ttl": "720h"})
role_id = call("GET", "auth/approle/role/litellm/role-id")["data"]["role_id"]
secret_id = call("POST", "auth/approle/role/litellm/secret-id")["data"]["secret_id"]
for name, value in (("role_id", role_id), ("secret_id", secret_id)):
    p = Path("/bootstrap/approle/litellm") / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.unlink(missing_ok=True)
    p.write_text(value)
    p.chmod(0o444)
admin = Path("/bootstrap/admin/master_key")
admin.parent.mkdir(parents=True, exist_ok=True)
admin.unlink(missing_ok=True)
admin.write_text(kv("gateway/runtime")["data"]["master_key"])
os.chown(admin, 10001, 10001)        # the demo container's non-root user, and nobody else
admin.chmod(0o400)
print("OpenBao configured for the gateway")
