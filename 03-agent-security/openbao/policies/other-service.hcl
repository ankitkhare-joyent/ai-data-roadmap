# A different service with its own audience. Its tokens must be useless at the fleet-ops MCP server.
path "identity/oidc/token/other-service" {
  capabilities = ["read"]
}
