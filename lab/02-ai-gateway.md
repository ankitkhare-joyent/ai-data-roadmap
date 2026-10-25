# Lab 02 · AI gateway: keys, limits, fallbacks, and what breaks under it

One sitting, about 40 minutes. Background: [lesson 2](../02-ai-gateway/README.md). Code:
[config.yaml](../02-ai-gateway/gateway/config.yaml), [entrypoint.py](../02-ai-gateway/gateway/entrypoint.py),
[bao_bootstrap.py](../02-ai-gateway/scripts/bao_bootstrap.py), [fake_llm/app.py](../02-ai-gateway/fake_llm/app.py).

After this lab you can explain:

- how the gateway gets its provider key and database password without either appearing in config
- what a virtual key can and can't do, and in what order the gateway checks allow-list, budget and rate limit
- what retries and fallbacks look like from the client, and from the provider's side
- what happens to caching and rate limits when Redis dies, and why rotating a password can be a time bomb

## Before you start

```bash
cd ~/workstuff/argocd-terraform-opentofu/ai-data-roadmap/02-ai-gateway
docker ps --format '{{.Names}}'          # Docker is shared: only the ec2-lakehouse-* baseline may be running
mise run reset                           # OpenBao, Postgres, Redis, fake provider, LiteLLM, all fresh (~1 min)
source ../lab/env.sh
M=$(gw_master)                           # the admin (master) key, held in a shell variable only
```

The provider behind the gateway is a fake Anthropic API: it costs nothing and its answers say which model served
them. Every request in this lab goes through the real LiteLLM.

## 1. How secrets reach the gateway

**Predict (P1):** the compose file starts LiteLLM. Which of `ANTHROPIC_API_KEY`, `LITELLM_MASTER_KEY` and
`DATABASE_URL` does the compose config contain? Which does the running process have?

```bash
docker compose config --format json litellm | jq -r '.services.litellm.environment | keys[]' | tr '\n' ' '; echo
docker compose exec -T litellm sh -c "tr '\0' '\n' < /proc/1/environ | cut -d= -f1 | grep -E '^(LITELLM_|ANTHROPIC_|DATABASE)' | sort"
docker compose exec -T litellm sh -c 'tr "\0" " " < /proc/1/cmdline; echo'
docker compose logs litellm | grep "secrets loaded"
```

## 2. Who owns the database password

```bash
export BAO_TOKEN=$(bao_root)
bao database/static-roles/litellm | jq -c '.data | {username, rotation_period, last_vault_rotation}'
```

**Predict (P2):** `compose.yaml` still says `POSTGRES_PASSWORD: pg-bootstrap-only`. Can you log in as `postgres`
with it from another container? And from *inside* the Postgres container?

```bash
docker compose run --rm -T --no-deps -e PGPASSWORD=pg-bootstrap-only --entrypoint psql postgres \
  -h postgres -U postgres -d litellm -c 'select 1' 2>&1 | grep -v Container | head -2
docker compose exec -T postgres psql -U postgres -d litellm -tAc "select count(*) from pg_stat_activity where usename='litellm'"
```

## 3. Aliases and a virtual key

```bash
curl -s $GW/v1/models -H "Authorization: Bearer $M" | jq -c '[.data[].id]'
K=$(curl -s $GW/key/generate -H "Authorization: Bearer $M" -H content-type:application/json \
  -d '{"key_alias":"lab-key","models":["cheap","fast"]}' | jq -r .key)
msg() {  # msg KEY MODEL TEXT [extra json]  -> POST /v1/messages through the gateway
  curl -s "${@:5}" $GW/v1/messages -H "x-api-key: $1" -H "anthropic-version: 2023-06-01" -H content-type:application/json \
    -d "{\"model\":\"$2\",\"max_tokens\":50,\"messages\":[{\"role\":\"user\",\"content\":\"$3\"}]${4:+,$4}}"
}
msg $K cheap "hello lab" "" -D /tmp/h.txt | jq -c '{model, text: .content[0].text}'
grep -i -E "x-litellm-(model-name|model-group|response-cost|attempted)" /tmp/h.txt | tr -d '\r'; rm /tmp/h.txt
```

**Predict (P3):** the app asked for `cheap`. What does the response body say the model is, and what do the headers
say? Then: what status does this key get for `smart`?

```bash
msg $K smart "hi" "" -o /dev/null -w "%{http_code}\n"
```

## 4. Rate limits and budgets

```bash
R=$(curl -s $GW/key/generate -H "Authorization: Bearer $M" -H content-type:application/json \
  -d '{"key_alias":"lab-rpm","models":["cheap"],"rpm_limit":3}' | jq -r .key)
for i in 1 2 3 4 5; do msg $R cheap "rpm $i $RANDOM" "" -o /dev/null -w "%{http_code} "; done; echo
```

**Predict (P4):** a key with a $0.005 budget; each `cheap` answer costs about $0.0022. Which call is the first
refused, and how much will the key have spent by then?

```bash
B=$(curl -s $GW/key/generate -H "Authorization: Bearer $M" -H content-type:application/json \
  -d '{"key_alias":"lab-budget","max_budget":0.005}' | jq -r .key)
for i in 1 2 3 4; do msg $B cheap "budget $i $RANDOM" "" -o /tmp/b.json -w "%{http_code} "; done; echo
jq -r .error.message /tmp/b.json | cut -c1-110; rm /tmp/b.json
```

## 5. Retries and fallback

Make the provider return `529 overloaded` for every Opus call, then ask for `smart` five times:

```bash
S=$(curl -s $GW/key/generate -H "Authorization: Bearer $M" -H content-type:application/json -d '{"key_alias":"lab-smart"}' | jq -r .key)
FAKE_FAIL_RATE=1.0 docker compose up -d --wait fake-llm
for i in 1 2 3 4 5; do
  msg $S smart "fb $i $RANDOM" "" -D /tmp/h.txt | jq -r '.content[0].text // .error.message' | cut -c1-40
  grep -i 'x-litellm-attempted-fallbacks' /tmp/h.txt | tr -d '\r'
done; rm /tmp/h.txt
docker compose exec -T fake-llm python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/stats').read().decode())"
```

**Predict (P5):** do the clients see any errors? How many Opus requests did the provider receive for 5 client
requests, and why that number? The config has `allowed_fails: 3` with a 30 s cooldown: did the cooldown kick in?

Put the provider back to normal before moving on: `docker compose up -d --wait fake-llm`.

## 6. The response cache

```bash
C=$(curl -s $GW/key/generate -H "Authorization: Bearer $M" -H content-type:application/json -d '{"key_alias":"lab-cache"}' | jq -r .key)
stats() { docker compose exec -T fake-llm python -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/stats')).get('haiku_requests',0))"; }
for i in 1 2; do b=$(stats); msg $C cheap "Classify: executor lost, retrying stage 3" "" -o /dev/null -w "%{time_total}s "; echo "provider calls $b -> $(stats)"; done
msg $C cheap "Classify: executor lost, retrying stage 3" '"cache":{"no-cache":true}' -o /dev/null -w "%{time_total}s "; echo "no-cache -> $(stats)"
```

**Predict (P6):** which of the three requests reach the provider?

## Break it, then fix it

**Break 1: Redis goes away.**

```bash
R2=$(curl -s $GW/key/generate -H "Authorization: Bearer $M" -H content-type:application/json -d '{"key_alias":"lab-redis","rpm_limit":3}' | jq -r .key)
docker compose stop redis
for i in 1 2 3 4 5; do msg $R2 cheap "same prompt" "" -o /dev/null -w "%{http_code}(%{time_total}s) "; done; echo
docker compose logs --since 2m litellm | grep -i redis | tail -2 | cut -c1-160
```

**Predict (P7):** do requests fail? Is the rate limit still enforced? Are identical prompts still served from cache?
What would be different with three gateway replicas?

**Fix:** `docker compose start redis`. The circuit breaker closes again after a short wait.

**Break 2: rotate the database password under a running gateway.**

```bash
bao database/rotate-role/litellm -X POST -o /dev/null -w "rotate: %{http_code}\n"
curl -s -o /dev/null -w "new key right after rotation: %{http_code}\n" $GW/key/generate -H "Authorization: Bearer $M" \
  -H content-type:application/json -d '{"key_alias":"after-rotate"}'
```

**Predict (P8):** the gateway still has the *old* password. Why does it keep working? Now restart Postgres, which
drops every connection:

```bash
docker compose restart postgres && sleep 6
curl -s -o /tmp/b.json -w "after postgres restart: %{http_code}\n" $GW/key/generate -H "Authorization: Bearer $M" \
  -H content-type:application/json -d '{"key_alias":"time-bomb"}'; jq -r '.error.message // .detail' /tmp/b.json | head -1
docker compose logs --since 1m postgres | grep -i "password authentication failed" | tail -1
```

**Fix:** restart the gateway, so the entrypoint reads the current password from OpenBao:

```bash
docker compose restart litellm && docker compose up -d --wait litellm
curl -s -o /dev/null -w "after gateway restart: %{http_code}\n" $GW/key/generate -H "Authorization: Bearer $M" \
  -H content-type:application/json -d '{"key_alias":"fixed"}'
```

When you're done: `mise run down`.

## Check your predictions

**P1.** The compose config has only `ANTHROPIC_API_BASE BAO_ADDR BAO_APPROLE_DIR LITELLM_LOG REDIS_HOST
STORE_MODEL_IN_DB`: no secret at all. PID 1 has `ANTHROPIC_API_KEY`, `DATABASE_URL`, `LITELLM_MASTER_KEY` and
`LITELLM_SALT_KEY`. Its command line is `litellm --config /app/config.yaml --port 4000`, not `entrypoint.py`: the
entrypoint logged in with AppRole, read the secrets, printed `gateway: secrets loaded from OpenBao, starting
LiteLLM`, and `exec`'d LiteLLM in its own place, environment included.

**P2.** From another container: `FATAL: password authentication failed for user "postgres"`. Bootstrap handed the
superuser to OpenBao and rotated it (`database/rotate-root`), so the value in `compose.yaml` is dead.
`litellm`'s password was set by OpenBao the moment the static role was created
(`last_vault_rotation` is the bootstrap time; `rotation_period` is 2592000 s, 30 days). From *inside* the
container, `psql -U postgres` works with no password at all: the official image trusts local socket
connections. Anyone who can `docker exec` (or `kubectl exec`) into a database is its superuser, which is why
production databases are managed services with no shell. The count query shows **3** open connections from
`litellm`: the gateway's pool.

**P3.** The body says `"model":"cheap"` and the text `[served by claude-haiku-4-5]`; the headers say
`x-litellm-model-name: anthropic/claude-haiku-4-5`, `x-litellm-model-group: cheap`, and the cost
(`x-litellm-response-cost: 0.002202`). The app only ever names the alias; the platform team can move `cheap` to
another model without touching it. `smart` with this key: **403** (not on the key's model allow-list).

**P4.** Rate limit: `200 200 200 429 429`, with `Rate limit exceeded for api_key ... Limit type: requests.
Current limit: 3`. Budget: `200 200 200 422` and `Budget has been exceeded! ... Current cost: 0.006609, Max
budget: 0.005`. The **third** call was allowed at $0.0044 spent, and pushed the key to $0.0066: budgets are checked
*before* a request against spend so far, so a key can overshoot by up to one request. Size budgets with that slack
in mind, especially for agents whose single calls can be large.

**P5.** No client errors: all five answers say `[served by claude-sonnet-5-5]`, each with
`x-litellm-attempted-fallbacks: 1`. The provider saw `{"opus_requests":10,"opus_overloaded":10,"sonnet_requests":5}`:
every client request tried Opus twice (the first call plus `num_retries: 1`), then fell back. The cooldown never
stopped the Opus calls, because `smart` has a single deployment and LiteLLM won't cool down the last deployment in
a group. Cooldowns only help when an alias has several deployments (exercise 1 in the lesson). Note too that the
`x-litellm-attempted-retries` header said 0 although the provider clearly saw a retry: check the provider's side
before trusting a gateway's headers.

**P6.** Only the first and the third: `provider calls 3 -> 4` (0.25 s), then `4 -> 4` in 0.02 s (served from
Redis), then the `no-cache` request goes through again (`-> 5`). Caching is per exact prompt and opt-out per
request.

**P7.** Nothing fails: `200 200 200 429 429`. LiteLLM opens a circuit breaker
(`RedisCircuitBreakerOpenError: Redis circuit breaker is open — skipping async_set_cache`) and carries on with
in-memory state. With one replica that's still correct. With three, each replica would count separately, so a
3 rpm key could make 9 requests a minute, and each would keep its own cache. Identical prompts take 0.1-0.2 s each
now: no cache. The gateway fails *open*, which keeps apps up and quietly weakens your limits; alert on it.

**P8.** Rotation returns `204` and new keys still work (`200`): Postgres authenticates a connection once, at
connect time, and changing a role's password doesn't drop existing sessions. The gateway's pool keeps running on
connections made with the old password. After the Postgres restart, the pool reconnects with the old password:
`500`, `Authentication failed against database server at postgres, the provided database credentials for litellm
are not valid`, and Postgres logs `password authentication failed for user "litellm"`. The failure surfaces at
the next reconnect, which could be days after the rotation. Restarting the gateway fixes it (`200`). In
Kubernetes, External Secrets plus a rollout on change does this automatically.

## What to remember

- Provider keys and passwords live in OpenBao and reach exactly one process, at start. Nothing in config.
- A virtual key is a policy: allow-list, budget, rate limit. Budgets can overshoot by one request.
- Fallbacks hide provider pain from apps; watch the provider-side numbers to see it.
- The gateway fails open when Redis dies. Shared state is what makes limits correct across replicas.
- A rotated secret isn't rolled out until every process holding the old one has restarted.

Next: [Lab 03 · Agent security](03-agent-security.md).
