# Lab 07 · Multi-agent and A2A: tasks on the wire, follow-ups, and failing well

One sitting, about 35 minutes, no Docker. Background: [lesson 7](../07-multi-agent-a2a/README.md). Code:
[specialists.py](../07-multi-agent-a2a/src/multi_agent/specialists.py), [remote.py](../07-multi-agent-a2a/src/multi_agent/remote.py),
[orchestrator.py](../07-multi-agent-a2a/src/multi_agent/orchestrator.py), [serve.py](../07-multi-agent-a2a/src/multi_agent/serve.py).

After this lab you can explain:

- what an Agent Card promises, and what an A2A task looks like as raw JSON-RPC
- how `input-required` turns one request into a multi-turn task, and when a task can't be continued
- how the orchestrator spends its delegation budget, and how it degrades when an agent is down or slow

## Before you start

You need two terminals in the lesson folder.

```bash
cd ~/workstuff/argocd-terraform-opentofu/ai-data-roadmap/07-multi-agent-a2a
mise trust && mise install
mise run reset
mise run serve                   # terminal 1: fleet-analyst on :49701, runbook-expert on :49702 (Ctrl-C to stop)
```

```bash
cd ~/workstuff/argocd-terraform-opentofu/ai-data-roadmap/07-multi-agent-a2a     # terminal 2
source ../lab/env.sh && source .lab-tokens        # $FLEET_TOKEN, $RUNBOOK_TOKEN
RB=http://127.0.0.1:49702/a2a/jsonrpc
send() {  # send URL TOKEN TEXT [extra message fields]  -> A2A SendMessage over JSON-RPC
  curl -s "$1" -H "Authorization: Bearer $2" -H content-type:application/json -H "A2A-Version: 1.0" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"SendMessage\",\"params\":{\"message\":{\"role\":\"ROLE_USER\",\"messageId\":\"$(uuidgen)\",\"parts\":[{\"text\":\"$3\"}]${4:+,$4}}}}"
}
```

## 1. The Agent Card

```bash
curl -s localhost:49702/.well-known/agent-card.json \
  | jq -c '{name, version, skills: [.skills[].id], interfaces: .supportedInterfaces, securitySchemes, securityRequirements}'
curl -s -o /dev/null -w "%{http_code}\n" $RB -H content-type:application/json -d '{}'
```

**Predict (P1):** the card is fetched with no token. What does it tell a caller before the first call? And the
JSON-RPC endpoint without a token?

## 2. A task that asks back

```bash
send $RB $RUNBOOK_TOKEN "the box is hot, what should I do" > /tmp/a2a.json
jq -c '.result.task | {id, contextId, state: .status.state, ask: .status.message.parts[0].text}' /tmp/a2a.json
TID=$(jq -r .result.task.id /tmp/a2a.json); CID=$(jq -r .result.task.contextId /tmp/a2a.json)
```

**Predict (P2):** the runbook agent needs an instance id. Does it guess, fail, or something else? What state is the
task in?

Answer its question on the *same* task, then read the task back:

```bash
send $RB $RUNBOOK_TOKEN "it is i-0a1b2c3d4e5f60002, cpu is hot" "\"taskId\":\"$TID\",\"contextId\":\"$CID\"" \
  | jq -c '.result.task | {id, state: .status.state, artifacts: [.artifacts[] | {name, text: .parts[0].text[:60]}]}'
curl -s $RB -H "Authorization: Bearer $RUNBOOK_TOKEN" -H content-type:application/json -H "A2A-Version: 1.0" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"GetTask\",\"params\":{\"id\":\"$TID\"}}" \
  | jq -c '.result | {state: .status.state, history: [.history[] | .role + ": " + (.parts[0].text[:45])]}'
```

**Predict (P3):** how many messages are in the task's history, and whose are they?

**Predict (P4):** the task is completed. What happens if you send another message to it?

```bash
send $RB $RUNBOOK_TOKEN "and also i-0a1b2c3d4e5f60001" "\"taskId\":\"$TID\",\"contextId\":\"$CID\"" | jq -c '.error'
send $RB $FLEET_TOKEN "hi"; echo                                    # fleet's token at the runbook agent
curl -s $RB -H "Authorization: Bearer $RUNBOOK_TOKEN" -H content-type:application/json \
  -d '{"jsonrpc":"2.0","id":3,"method":"message/send","params":{}}' | jq -c .error     # the A2A 0.3 method name
```

## 3. The orchestrator against the live agents

```bash
uv run python -c "
import asyncio, os
from multi_agent.orchestrator import Orchestrator, Budget
urls = {'fleet-analyst': 'http://127.0.0.1:49701', 'runbook-expert': 'http://127.0.0.1:49702'}
tok = {'fleet-analyst': os.environ['FLEET_TOKEN'], 'runbook-expert': os.environ['RUNBOOK_TOKEN']}
Q = 'Which instance is hot right now, and what should I do to fix it?'
async def main():
    for label, o in (('default', Orchestrator(urls, tok)), ('budget 2', Orchestrator(urls, tok, budget=Budget(2))),
                     ('bad fleet token', Orchestrator(urls, {**tok, 'fleet-analyst': 'nope'}))):
        r = await o.answer(Q)
        print('==', label, '| delegations', r.delegations, '|', [(s.agent, s.state) for s in r.spans])
        print('  ', r.answer.replace(chr(10), ' / ')[:160])
    p = await Orchestrator(urls, tok).answer(Q); s = await Orchestrator(urls, tok).answer(Q, parallel=False)
    print('parallel', round(p.seconds, 2), 's | sequential', round(s.seconds, 2), 's')
asyncio.run(main())"
```

**Predict (P5):** with the default budget (4), how many delegations does the question take, and in what order do
the spans appear? With a budget of 2, what's missing from the answer?

**Predict (P6):** with a wrong token for fleet-analyst, does the whole request fail? How many delegations does it
use?

**Predict (P7):** each agent takes 0.4 s. How long does the fan-out take in parallel and sequentially?

## Break it, then fix it

**Break: a slow specialist.** Stop `serve` in terminal 1 (Ctrl-C); this one runs the agents in-process so it can make
fleet-analyst take 3 s:

```bash
uv run python -c "
import asyncio
from multi_agent import specialists
from multi_agent.demo import specialists as running
from multi_agent.orchestrator import Orchestrator
specialists.FleetAnalyst.latency_s = 3.0
async def main():
    async with running() as (urls, tok):
        for t in (10, 1):
            r = await Orchestrator(urls, tok, timeout_s=t).answer('Which instance is hot right now, and what should I do to fix it?')
            print('timeout', t, '| took', round(r.seconds, 2), 's |', [(s.agent, s.state) for s in r.spans])
asyncio.run(main())" 2>&1 | grep timeout
```

**Predict (P8):** with a 10 s deadline, how long does the request take? With 1 s, what does the user get?

**Fix:** there's nothing to repair in the code: the deadline is the fix. Choose it from the specialist's p95 latency,
and make the answer say what's missing (it does: `unavailable (TIMEOUT); answer may be incomplete`).

## Check your predictions

**P1.** Name, version, skills (`["runbook"]`), the endpoint and binding
(`{"url":"http://127.0.0.1:49702/a2a/jsonrpc","protocolBinding":"JSONRPC","protocolVersion":"1.0"}`) and how to
authenticate (`securitySchemes: {"bearer": {"httpAuthSecurityScheme": {"scheme":"Bearer","bearerFormat":"JWT"}}}`,
required). Discovery is public on purpose; the endpoint returns `401` without a token.

**P2.** Neither: it returns the task in `TASK_STATE_INPUT_REQUIRED` with the question
`Which instance id is this about? I need it to pick the right runbook.` The task stays open, waiting for more
input on the same `taskId`.

**P3.** Five, alternating: `ROLE_USER: the box is hot…`, `ROLE_AGENT: Finding the runbook...`, `ROLE_AGENT: Which
instance id is this about?…`, `ROLE_USER: it is i-0a1b2c3d4e5f60002…`, `ROLE_AGENT: Finding the runbook...`. The
answer itself is an **artifact** (`runbook`: `RB-12 High CPU: …`), kept separate from the chatty status messages.

**P4.** Refused: `{"code":-32004,"message":"Task … is in terminal state: 3", … "reason":"UNSUPPORTED_OPERATION"}`.
Completed, failed, canceled and rejected are terminal; new work is a new task (same `contextId` if it belongs to
the same conversation). Fleet's token at the runbook agent: `{"error":"unauthorized"}`: each agent accepts only its
own credential. The 0.3-era method name `message/send` gets `-32601 Method not found`: A2A 1.0 renamed the methods
(`SendMessage`, `GetTask`, …), and the SDK only serves the 0.3 names if you enable compatibility.

**P5.** Three delegations: `runbook-expert INPUT_REQUIRED` and `fleet-analyst COMPLETED` in parallel, then
`runbook-expert COMPLETED` on the same task, after the orchestrator answered the follow-up with the hot instance
fleet-analyst found (`i-0a1b2c3d4e5f60002`). With a budget of 2 there's no budget left for the follow-up, so the
answer says `[runbook-expert] needs more information: Which instance id is this about?…`: honest, but incomplete.

**P6.** No. The card is public, so discovery works, and the call then fails with
`HTTP Error 401: Client error '401 Unauthorized'`; the answer says `[fleet-analyst] unavailable (ERROR); answer may
be incomplete`. It uses **2** delegations: with no instance id from fleet-analyst, there's nothing to answer the
runbook agent's question with, so the orchestrator doesn't spend budget on a follow-up it can't make. (The first
version spent the budget anyway; this lab caught it.)

**P7.** About **0.88 s** in parallel and **1.31 s** sequentially: the follow-up must wait for fleet-analyst either way,
so parallelism saves one agent's latency, not two.

**P8.** With 10 s it takes about **3.57 s**, all three spans complete: the deadline is a ceiling, not a wait. With 1 s it
returns in about **1.06 s** with `fleet-analyst TIMEOUT`, and the runbook question stays unanswered because the
fact it needed never arrived. A slow dependency degrades one part of the answer instead of hanging the request.

## What to remember

- The Agent Card is the contract: skills, endpoint, auth. Callers learn everything else from tasks.
- `input-required` is how agents ask instead of guessing; follow-ups continue the same task until it's terminal.
- Budget and deadlines belong to the orchestrator. Spend budget only on work that can succeed.
- Every downstream failure becomes a sentence in the answer, never a hung request.

Next: [Lab 08 · Agentic data engineering](08-agentic-data-eng.md).
