# Lab 01 · Durable agents: what Temporal records, and what survives a crash

One sitting, about 40 minutes. Background: [lesson 1](../01-durable-agents/README.md). Code:
[workflow.py](../01-durable-agents/src/durable_agent/workflow.py),
[activities.py](../01-durable-agents/src/durable_agent/activities.py),
[codec.py](../01-durable-agents/src/durable_agent/codec.py),
[fleet.py](../01-durable-agents/src/durable_agent/fleet.py).

After this lab you can explain:

- what Temporal writes to history for one agent turn, and why a crashed worker doesn't call the model again
- why a human approval can wait a day without holding a thread, a connection or a pod
- what happens when you deploy workflow code that no longer matches a running workflow's history
- how payload encryption keys rotate, and what a deleted key version does to old history

Answers to every **Predict** are at the bottom. Write your guess down before you run the command.

## Before you start

```bash
cd ~/workstuff/argocd-terraform-opentofu/ai-data-roadmap/01-durable-agents
docker ps --format '{{.Names}}'          # Docker is shared: only the ec2-lakehouse-* baseline may be running
mise run reset                           # wipes OpenBao, Temporal history and the fleet, then starts fresh
source ../lab/env.sh
```

Keep the Temporal UI open at <http://localhost:49234> in a browser tab; it shows the same history as `events`.

## 1. OpenBao: who can read what

**Predict (P1):** this OpenBao has never been unsealed by a human. Is it sealed? What kind of seal is it, and
what does the bootstrap get back instead of unseal keys?

```bash
curl -s $BAO_ADDR/v1/sys/health | jq -c '{initialized, sealed, version}'
curl -s $BAO_ADDR/v1/sys/seal-status | jq -c '{type, recovery_seal, storage_type}'
```

Now look at the operator CLI's policy and at the codec key's versions (the root token lives in the `bao-init`
volume; `bao_root` reads it into a variable without printing it):

```bash
export BAO_TOKEN=$(bao_root)
bao sys/policies/acl/agent-cli | jq -r .data.policy | grep -v '^#'
bao secret/metadata/durable-agent/codec | jq -c '{current_version: .data.current_version}'
```

**Predict (P2):** you haven't put an Anthropic key in `.env`, so `secret/durable-agent/anthropic` doesn't exist.
When the CLI's AppRole tries to read it, will it get 404 (not found) or 403 (forbidden)?

```bash
docker compose run --rm -T --entrypoint python cli -c "
import asyncio; from durable_agent.bao import BaoClient
async def m():
    try: await BaoClient().kv_get('durable-agent/anthropic')
    except Exception as e: print(type(e).__name__, str(e)[:110])
asyncio.run(m())" 2>/dev/null
```

## 2. One run, as history

```bash
cli start --id lab-1
sleep 4
events lab-1
cli status lab-1 | jq -c '{phase, turn, llm_calls, pending: [.pending[].call.input.instance_id]}'
```

**Predict (P3):** the agent has called the model, listed idle instances, called the model again, and is now waiting
for you. How many `ACTIVITY_TASK_SCHEDULED` events, and how many timers? Which instance ids are pending, and why
is the idle **prod** box (`...0002`) not among them?

**Predict (P4):** what happens if you start `lab-1` again while it's waiting?

```bash
docker compose run --rm -T cli start --id lab-1 </dev/null 2>&1 | tail -1
```

Look at the timer that's waiting for you:

```bash
docker compose exec -T temporal temporal workflow show -w lab-1 --output json \
  | jq -c '.events[] | select(.eventType=="EVENT_TYPE_TIMER_STARTED") | .timerStartedEventAttributes'
```

## 3. Kill the worker

```bash
docker compose kill worker                       # SIGKILL: no graceful shutdown
docker compose run --rm -T cli status lab-1 </dev/null 2>&1 | grep -o "RPCError: .*"
```

**Predict (P5):** the workflow is still in Temporal. Why does a query fail now, when the history is intact?

```bash
docker compose up -d --wait worker
cli status lab-1 | jq -c '{phase, llm_calls, pending: (.pending|length)}'
docker compose logs worker | grep -c llm_step
```

**Predict (P6):** the new worker process has no memory of the run. How does it know there are 3 pending approvals,
and how many `llm_step` lines will the log show in total (before and after the kill)?

## 4. The human in the loop

Try two bad decisions first:

```bash
cli approve lab-1 toolu_nope --reviewer ankit
cli approve lab-1 toolu_stop_0001 --reviewer " "
events lab-1 | grep -i update || echo "(no update events)"
```

**Predict (P7):** both are refused. Do refused updates leave any trace in the workflow's history?

Now decide for real:

```bash
cli approve lab-1 toolu_stop_0001 --reviewer ankit
cli approve lab-1 toolu_stop_0003 --reviewer ankit
cli reject  lab-1 toolu_stop_0004 --reviewer ankit --reason "training next week"
sleep 4
cli result lab-1 | jq -c '{outcome, llm_calls, actions: (.actions|length)}'
docker compose logs worker | grep "run_tool stop_instance" | sed 's/.*run_tool/run_tool/'
```

The compose file sets `FLEET_FLAKY_STOPS=1`: the first attempt of every stop succeeds at the "cloud API" but the
response is lost, so Temporal retries it.

**Predict (P8):** the log shows four stop attempts for two approved stops. How many stop records are in the fleet's
state, and how many instances end up stopped by this run?

```bash
docker compose exec -T worker python -c "import json;s=json.load(open('/data/fleet-state.json')); \
print(len(s['stops']), [i['instance_id'][-4:]+':'+i['state'] for i in s['instances']])"
```

Retries don't add history events; the attempt number lives on the event:

```bash
docker compose exec -T temporal temporal workflow show -w lab-1 --output json \
  | jq -c '[.events[] | select(.eventType=="EVENT_TYPE_ACTIVITY_TASK_STARTED") | .activityTaskStartedEventAttributes.attempt]'
```

## 5. What the Temporal server actually stores

**Predict (P9):** the workflow input was the goal text. What does the server hold for it?

```bash
docker compose exec -T temporal temporal workflow show -w lab-1 --output json > /tmp/lab1.json
jq -c '.events[0].workflowExecutionStartedEventAttributes.input.payloads[0].metadata | map_values(@base64d)' /tmp/lab1.json
jq -r '.events[0].workflowExecutionStartedEventAttributes.input.payloads[0].data' /tmp/lab1.json | base64 -d | xxd | head -2
rm /tmp/lab1.json
```

## Break it, then fix it

**Break 1: deploy code that disagrees with history.** Start a second run, let it reach the approval step, then add
one line in front of the loop and redeploy the worker:

```bash
cli start --id lab-2 && sleep 4 && cli status lab-2 | jq -c '{phase, pending: [.pending[].call.id]}'
sed -i '' 's/^        messages: list\[dict\] = \[{"role": "user", "content": req.goal}\]$/&\
        await workflow.sleep(1)   # LAB BREAK/' src/durable_agent/workflow.py
grep -n "LAB BREAK" src/durable_agent/workflow.py
docker compose up -d --build --wait worker
docker compose run --rm -T cli approve lab-2 all --reviewer ankit </dev/null 2>&1 | tail -1
```

**Predict (P10):** how many instances does `lab-2` propose to stop, and why? Then: what error do you get, is the
workflow lost, and did anything get added to its history?

```bash
events lab-2
docker compose logs --no-color worker | grep -o "Nondeterminism error: [^\"]*" | sort -u
```

**Fix:** revert the change and redeploy. The waiting workflow picks up exactly where it was.

```bash
git checkout -- src/durable_agent/workflow.py
docker compose up -d --build --wait worker
cli approve lab-2 all --reviewer ankit && sleep 4
cli result lab-2 | jq -c '{outcome, llm_calls, actions: (.actions|length)}'
```

**Break 2: rotate the payload key, then lose the old version.**

```bash
docker compose run --rm bao-bootstrap python -m durable_agent.bao_bootstrap --rotate-codec 2>&1 | grep "codec version"
bao secret/metadata/durable-agent/codec | jq -c '{current_version: .data.current_version}'
cli result lab-1 | jq -c '{outcome}'                    # lab-1 was encrypted with v1
```

**Predict (P11):** new payloads now use `v2`. Does `lab-1` still decrypt? Then soft-delete version 1:

```bash
bao secret/delete/durable-agent/codec -X POST -d '{"versions":[1]}' -o /dev/null -w "%{http_code}\n"
docker compose run --rm -T cli result lab-1 </dev/null 2>&1 | grep Error | tail -1 | cut -c1-120
```

**Fix:** KV v2 deletes are soft. Undelete and read again:

```bash
bao secret/undelete/durable-agent/codec -X POST -d '{"versions":[1]}' -o /dev/null -w "%{http_code}\n"
cli result lab-1 | jq -c '{outcome}'
```

When you're done: `mise run down` (or go straight to the next lab; each lab resets its own stack).

## Check your predictions

**P1.** `{"initialized":true,"sealed":false,"version":"2.7.1"}` and
`{"type":"static","recovery_seal":true,"storage_type":"raft"}`. The static seal (a key file on its own volume,
standing in for a cloud KMS) unseals OpenBao at every start, so there are no unseal keys at all: initialisation
returns **recovery** keys, which are only needed for a few operator tasks such as generating a new root token.

**P2.** **403**, even though the secret doesn't exist:
`BaoError read durable-agent/anthropic failed: HTTP 403 {"errors":["1 error occurred:\n\t* permission denied\n\n"]}`.
The policy check happens before the lookup, so a token can't use 404 vs 403 to discover which secrets exist. The
`agent-cli` policy grants exactly one path, `secret/data/durable-agent/codec`, `read`.

**P3.** Three activities (`llm_step`, `run_tool` for `list_idle_instances`, `llm_step`), four workflow tasks, and
one `TIMER_STARTED`: the 24-hour approval timeout. Pending: `...0001`, `...0003`, `...0004`. The prod box is idle
too, but the agent was told to propose only `dev`/`test`, and if it tried anyway, `fleet.stop` refuses prod.
Defence in depth: the prompt is a policy, the API is the enforcement.

**P4.** `WorkflowAlreadyStartedError: Workflow execution already started`. The workflow id is the business key, so
"start" is idempotent: a double-clicked button or a retried cron can't start a second cleanup.

The timer: `{"timerId":"1","startToFireTimeout":"86400s"}`. A row in Temporal's database, not a sleeping thread.

**P5.** `RPCError: (9, 'no poller seen for task queue recently, worker may be down', b'')`. Queries run *workflow
code* to read its in-memory state, and only a worker can run workflow code. The history is fine; nobody is
available to replay it.

**P6.** It replays the history: re-runs the workflow function from the top, and every `execute_activity` whose
result is already in history returns that recorded result instantly instead of running. So it rebuilds
`_pending` exactly as before. Status shows `{"phase":"awaiting_approval","llm_calls":2,"pending":3}` and the log
still counts **2** `llm_step` lines, both from before the kill: replay called the model zero times.

**P7.** No trace. `toolu_nope: refused: ValueError: nothing pending with id toolu_nope` and
`reviewer is required for the audit trail` come from the update's **validator**, which runs before anything is
written. The grep prints `(no update events)`.

**P8.** `2` stop records, and `0001` and `0003` stopped (`0007` was already stopped in the seed data). The second
attempt of each stop found its idempotency key (`lab-1/toolu_stop_0001`) in the dedupe table and returned the first
result. Result: `{"outcome":"completed","llm_calls":3,"actions":2}`, and the attempt list is `[1,1,1,2,2,1]`: the
two stops show `attempt: 2`, the third model call and the list call show 1. At the end of the run the history has
6 activities, 3 accepted and 3 completed updates, a started and a canceled timer.

**P9.** Ciphertext: metadata `{"encoding":"binary/encrypted","encryption-key-id":"v1"}` and random-looking bytes.
The codec runs in the client and the worker, so the Temporal server and its database never see prompts, tool
output or answers. The key id is bound into the AES-GCM associated data, so editing the metadata breaks decryption.

**P10.** `lab-2` proposes **one** stop, `...0004`: lab-1 already stopped `0001` and `0003`, and the agent lists
*running* idle instances. State lives in the fleet, not in the agent. Approving then fails with
`WorkflowQueryFailedError: [TMPRL1100] Nondeterminism error: Timer machine does not handle this event:
HistoryEvent(id: 5, ActivityTaskScheduled)`: the new code asks for a timer where history says an activity was
scheduled. The workflow isn't lost, and history is unchanged (still 3 activities, 1 timer); the worker keeps
failing the workflow task until the code matches again. After the revert, `approve all` approves `toolu_stop_0004`
and the result is `{"outcome":"completed","llm_calls":3,"actions":1}`. In real life you'd wrap the change in
`workflow.patched("...")` so old runs keep the old path and new runs take the new one.

**P11.** Yes: `{"outcome":"completed"}`. Each payload names the key version that encrypted it, and the codec
fetches older versions on demand, so rotation never needs re-encryption. After the soft delete, reading `lab-1`
fails with `BaoError: read durable-agent/codec failed: HTTP 404`: the history is intact but unreadable. Undelete
returns `204` and the result reads again. If you had *destroyed* the version instead (`secret/destroy/...`), that
history would be gone for good. Treat payload keys like backups' keys: never destroy a version that history still
references.

## What to remember

- Workflow code decides, activities do. The history records every decision's result, so recovery is replay, not
  re-execution.
- Retries are only safe with idempotency keys downstream; Temporal guarantees at-least-once, not exactly-once.
- Deploying workflow code is a schema change for running workflows. Version it (`workflow.patched`).
- Encrypt payloads client-side, and manage the keys' versions as carefully as the data.

Next: [Lab 02 · AI gateway](02-ai-gateway.md).
