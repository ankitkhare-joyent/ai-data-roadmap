# The triage-agent workload may mint identity JWTs for its own OIDC role and nothing else. The role fixes the
# audience (the fleet-ops MCP server) and the scope claim, so the agent cannot ask for more than it was given.
path "identity/oidc/token/triage-agent" {
  capabilities = ["read"]
}
