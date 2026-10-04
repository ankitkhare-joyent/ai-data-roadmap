# The Temporal worker: runs the LLM and tool activities. OpenBao is deny-by-default.

# The Anthropic key, read inside the llm_step activity (never inside workflow code or history).
path "secret/data/durable-agent/anthropic" {
  capabilities = ["read"]
}

# The payload-encryption key. Any version may be read, so history written under an old key still decrypts.
path "secret/data/durable-agent/codec" {
  capabilities = ["read"]
}
