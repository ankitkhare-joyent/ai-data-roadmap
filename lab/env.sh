# Source once per terminal, from inside a lesson folder (bash or zsh):
#
#   cd 01-durable-agents && source ../lab/env.sh
#
# Sets the lesson's local URLs and defines a few helpers. Nothing here prints a secret; the functions that
# return one (bao_root) are for use inside $( ... ) only.

case "$(basename "$PWD")" in
  01-durable-agents) export BAO_ADDR=http://localhost:49200 TEMPORAL_UI=http://localhost:49234 ;;
  02-ai-gateway)     export BAO_ADDR=http://localhost:49401 GW=http://localhost:49400 ;;
  03-agent-security) export BAO_ADDR=http://localhost:49300 MCP=http://localhost:49301/mcp PYTHONPATH=src:. ;;
  04-llm-serving-k8s) export ROUTER=http://localhost:49480 ;;
  0[5-9]-*)          export PYTHONPATH=src ;;
  *) echo "lab/env.sh: cd into a lesson folder first (01-durable-agents, 02-ai-gateway, ...)" >&2 ;;
esac

# The OpenBao root token, read from the bao-init volume where bootstrap kept it (LOCAL ONLY: production revokes
# the root token after setup). Use as:  BAO_TOKEN=$(bao_root)
bao_root() {
  docker compose run --rm -T --no-deps --entrypoint cat bao-bootstrap /bootstrap/init/init.json 2>/dev/null \
    | jq -r .root_token
}

# bao GET path [extra curl args]   -> OpenBao API call with $BAO_TOKEN (set it first with bao_root)
bao() { curl -s -H "X-Vault-Token: $BAO_TOKEN" "$BAO_ADDR/v1/$1" "${@:2}"; }

# jwt_claims TOKEN                 -> a JWT's decoded payload (no signature check: for reading only)
jwt_claims() {
  python3 -c 'import sys,json,base64;p=sys.argv[1].split(".")[1];print(json.dumps(json.loads(base64.urlsafe_b64decode(p+"="*(-len(p)%4))),indent=2))' "$1"
}

# cli ...                          -> lesson 1's operator CLI inside the compose network (quiet compose output)
cli() { docker compose run --rm -T cli "$@" </dev/null 2>/dev/null; }

# events WORKFLOW_ID               -> lesson 1: event types in a workflow's history, with counts
events() {
  docker compose exec -T temporal temporal workflow show -w "$1" --output json \
    | jq -r '.events[].eventType' | sed 's/EVENT_TYPE_//' | sort | uniq -c | sort -rn
}

# gw_master                        -> lesson 2: the LiteLLM master key, for use inside $( ... ) only
gw_master() {
  docker compose run --rm -T --no-deps --entrypoint cat bao-bootstrap /bootstrap/admin/master_key 2>/dev/null
}

# mint ROLE                        -> lesson 3: a fresh OpenBao identity JWT for triage-agent | ops-agent | other-service
mint() {
  docker compose run --rm -T --no-deps --entrypoint python demo -c "
import asyncio; from pathlib import Path; from secure_agent.auth import openbao_token
d = Path('/bootstrap/approle/$1')
print(asyncio.run(openbao_token('http://openbao:8200', (d/'role_id').read_text().strip(), (d/'secret_id').read_text().strip(), '$1')))
" 2>/dev/null | tail -1
}

# mcp_rpc TOKEN METHOD [PARAMS_JSON] -> lesson 3: one MCP JSON-RPC call (opens a session, then sends the call)
mcp_rpc() {
  local params="${3:-}"; [ -n "$params" ] || params='{}'
  local h=(-H "Authorization: Bearer $1" -H "content-type: application/json" -H "accept: application/json, text/event-stream"
           -H "mcp-protocol-version: 2025-11-25")
  local sid=$(curl -s -D - -o /dev/null "${h[@]}" "$MCP" -d '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"lab","version":"1"}}}' \
    | tr -d '\r' | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}')
  curl -s "${h[@]}" -H "mcp-session-id: $sid" "$MCP" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' -o /dev/null
  curl -s "${h[@]}" -H "mcp-session-id: $sid" "$MCP" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$2\",\"params\":$params}" \
    | sed -n 's/^data: //p' | head -1
}
