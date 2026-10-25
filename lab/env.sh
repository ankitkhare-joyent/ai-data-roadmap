# Source once per terminal, from inside a lesson folder (bash or zsh):
#
#   cd 01-durable-agents && source ../lab/env.sh
#
# Sets the lesson's local URLs and defines a few helpers. Nothing here prints a secret; the functions that
# return one (bao_root) are for use inside $( ... ) only.

case "$(basename "$PWD")" in
  01-durable-agents) export BAO_ADDR=http://localhost:49200 TEMPORAL_UI=http://localhost:49234 ;;
  02-ai-gateway)     export BAO_ADDR=http://localhost:49401 GW=http://localhost:49400 ;;
  03-agent-security) export BAO_ADDR=http://localhost:49300 MCP=http://localhost:49301/mcp ;;
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
