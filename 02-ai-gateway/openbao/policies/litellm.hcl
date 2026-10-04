# The LiteLLM gateway. It is the ONLY workload that can read provider keys: every team and app gets a
# LiteLLM virtual key instead, which can be budgeted, rate limited and revoked without touching Anthropic.

# Provider keys, stored as {"key": "..."} at secret/litellm/<NAME> (the layout LiteLLM's Enterprise Vault
# integration expects, so switching to it later needs no data migration).
path "secret/data/litellm/*" {
  capabilities = ["read"]
}

# Boot-time settings (master key, salt key, database URL), rendered into env by gateway/entrypoint.py.
path "secret/data/gateway/runtime" {
  capabilities = ["read"]
}

# The gateway's Postgres password: a static role whose password OpenBao rotates (every 30 days here).
# The entrypoint reads it at boot; a rolling restart after rotation picks up the new one.
path "database/static-creds/litellm" {
  capabilities = ["read"]
}
