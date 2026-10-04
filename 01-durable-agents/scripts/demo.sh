#!/usr/bin/env bash
# Walkthrough: start a run, crash the worker while it waits for approval, restart, approve, check the result.
set -euo pipefail
cd "$(dirname "$0")/.."
cli() { docker compose run --rm -T cli "$@" </dev/null 2>/dev/null; }   # compose would eat the loop's stdin
id="cleanup-$(date +%H%M%S)"

echo "== 1. start the agent ($id)"
cli start --id "$id"
for _ in $(seq 30); do
  phase=$(cli status "$id" | python3 -c 'import sys,json; print(json.load(sys.stdin)["phase"])')
  [ "$phase" = awaiting_approval ] && break; sleep 1
done
cli status "$id"

echo "== 2. kill the worker hard (no graceful shutdown) while the agent waits for a human"
docker compose kill worker
sleep 2
echo "   the workflow is still there, just waiting:"
cli status "$id" || echo "   (no worker to answer queries right now, that is expected)"

echo "== 3. bring the worker back; it replays history instead of calling the LLM again"
docker compose up -d --wait worker
cli status "$id"

echo "== 4. approve two stops, reject the GPU box"
pending=$(cli status "$id" | python3 -c '
import sys,json
for p in json.load(sys.stdin)["pending"]: print(p["call"]["id"], p["call"]["input"]["instance_id"])')
while read -r tid iid; do
  if [ "$iid" = i-0a1b2c3d4e5f60004 ]; then cli reject "$id" "$tid" --reviewer ankit --reason "training run next week"
  else cli approve "$id" "$tid" --reviewer ankit; fi
done <<< "$pending"

echo "== 5. result"
cli result "$id"

echo "== 6. what the server stored: event types only, payloads are ciphertext"
docker compose exec -T temporal temporal workflow show -w "$id" --output json \
  | python3 -c '
import sys,json,collections
ev=json.load(sys.stdin)["events"]
print(collections.Counter(e["eventType"] for e in ev).most_common())
import base64
def datas(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "data" and isinstance(v, str): yield v
            else: yield from datas(v)
    elif isinstance(o, list):
        for v in o: yield from datas(v)
raw = b"".join(base64.b64decode(d + "==") for d in datas(ev))
print("payloads:", sum(1 for _ in datas(ev)), "| plaintext \"instance\" visible to the server:", b"instance" in raw)'
echo "== worker log: retries of the flaky stop and the idempotent dedupe"
docker compose logs worker 2>&1 | grep -E "run_tool stop_instance|attempt=2" | tail -6
