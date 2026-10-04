"""Fetch boot-time secrets from OpenBao, put them in this process's environment, exec LiteLLM.

Everything lands in this process's environment only: never in a file, an image or compose.yaml.
LiteLLM's own Vault integration (which would refresh keys at runtime) is an Enterprise feature, so the
open-source pattern is render-at-boot: rotating a key in OpenBao takes effect on the next rolling restart.
In Kubernetes the same job is done by External Secrets Operator or the OpenBao Agent injector.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

addr = os.environ["BAO_ADDR"]
d = Path(os.environ.get("BAO_APPROLE_DIR", "/run/secrets/bao"))
role_id, secret_id = (d / "role_id").read_text().strip(), (d / "secret_id").read_text().strip()


def call(path: str, body: dict | None = None, token: str | None = None) -> dict:
    req = urllib.request.Request(f"{addr}/v1/{path}", data=json.dumps(body).encode() if body else None,
                                 headers={"X-Vault-Token": token} if token else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


token = call("auth/approle/login", {"role_id": role_id, "secret_id": secret_id})["auth"]["client_token"]
runtime = call("secret/data/gateway/runtime", token=token)["data"]["data"]
db = call("database/static-creds/litellm", token=token)["data"]          # password rotated by OpenBao
anthropic_key = call("secret/data/litellm/ANTHROPIC_API_KEY", token=token)["data"]["data"]["key"]
os.environ.update({
    "LITELLM_MASTER_KEY": runtime["master_key"],
    "LITELLM_SALT_KEY": runtime["salt_key"],      # encrypts provider creds LiteLLM stores in its DB
    "DATABASE_URL": f"postgresql://{db['username']}:{db['password']}@postgres:5432/litellm",
    "ANTHROPIC_API_KEY": anthropic_key,
})
print("gateway: secrets loaded from OpenBao, starting LiteLLM", flush=True)
os.execvp("litellm", ["litellm", "--config", "/app/config.yaml", "--port", "4000", *sys.argv[1:]])
