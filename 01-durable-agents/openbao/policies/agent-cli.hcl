# The operator CLI: starts runs, approves or rejects actions, reads results.
# It needs the codec key (to encrypt inputs and decrypt results) and nothing else: no model key.
path "secret/data/durable-agent/codec" {
  capabilities = ["read"]
}
