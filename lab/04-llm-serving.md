# Lab 04 · LLM serving: KV caches, batching, routing, and what a dead replica looks like

One sitting, about 40 minutes. Background: [lesson 4](../04-llm-serving-k8s/README.md). Code:
[sizing.py](../04-llm-serving-k8s/src/serving_lab/sizing.py), [router.py](../04-llm-serving-k8s/src/serving_lab/router.py),
[bench.py](../04-llm-serving-k8s/src/serving_lab/bench.py), [compose.yaml](../04-llm-serving-k8s/compose.yaml).

After this lab you can explain:

- why batching is what makes self-hosted tokens cheap, and where the KV cache limits it
- what a prefix-cache hit is worth, measured, and why round-robin throws it away
- where a single replica's queue starts to hurt, and how that number becomes an autoscaling threshold
- what a router without health checks does when a replica dies, and what an undersized KV cache does under load

## Before you start

```bash
cd ~/workstuff/argocd-terraform-opentofu/ai-data-roadmap/04-llm-serving-k8s
docker ps --format '{{.Names}}'          # Docker is shared: only the ec2-lakehouse-* baseline may be running
mise trust && mise install && mise run sync
mise run reset                           # three GPU-free vLLM simulators + the router, empty caches
source ../lab/env.sh
```

## 1. Sizing on paper

```bash
uv run python -c "
from serving_lab.sizing import *
p = Plan(MODELS['llama-3.1-8b'], GPUS['h100'], 1)
print('KV bytes/token', p.kv_bytes_per_token, '| tokens that fit', p.kv_tokens, '| 4k-token requests', p.max_concurrency(4096))"
```

**Predict (P1):** Llama 3.1 8B has 32 layers, 8 KV heads, head dimension 128, BF16. Work out the KV bytes per token
by hand before you run it. How many 4k-token requests fit on one H100 next to the weights?

**Predict (P2):** Qwen3 32B on one H100: how many 4k requests fit in BF16, and how many with FP8 weights and KV?

```bash
uv run python -c "
from serving_lab.sizing import *
bf = Plan(MODELS['qwen3-32b'], GPUS['h100'], 1)
f8 = Plan(MODELS['qwen3-32b'], GPUS['h100'], 1, weights_dtype='fp8', kv_dtype='fp8')
print('bf16', bf.max_concurrency(4096), '| fp8', f8.max_concurrency(4096))"
```

**Predict (P3):** for the 8B model, how much faster per token is one decode step at batch 1 than at batch 64, and how
much cheaper per million tokens is batch 64?

```bash
uv run python -c "
from serving_lab.sizing import *
p = Plan(MODELS['llama-3.1-8b'], GPUS['h100'], 1)
for b in (1, 8, 32, 64): print(b, round(p.decode_step_ms(b, 4096), 1), 'ms/step', round(p.output_tokens_per_s(b, 4096)), 'tok/s', round(usd_per_million_output_tokens(p, b, 4096), 2), '\$/M')"
```

## 2. What a model server exposes

```bash
sim 1 /metrics | grep -E "^vllm:(num_requests|kv_cache_usage)"
```

These are the signals an endpoint picker and an autoscaler read. Note what's *not* there: CPU.

## 3. A prefix-cache hit, measured

`chat TENANT ROUTE` sends one streamed request whose 1,500-word system prompt depends only on the tenant, then prints
the backend, the time to first token and how many prompt tokens the server already had cached.

```bash
for i in 1 2 3; do chat acme prefix_aware; done
chat globex prefix_aware
```

**Predict (P4):** what's the TTFT of the first `acme` request against the next two, and what's `cached_prompt_tokens`?
Where does `globex` land, and is it fast?

```bash
for i in 1 2 3 4; do chat initech round_robin; done
```

**Predict (P5):** same tenant, round-robin across three replicas. On which request does the first cache hit
happen?

## 4. Where a replica saturates

```bash
docker compose run --rm -T bench python -m serving_lab.bench --url http://router:8080 --concurrency 1 6 24 --sessions 24 \
  2>&1 | grep concurrency
```

**Predict (P6):** each simulator has 4 batch slots, so the pool has 12. What happens to TTFT p50, ITL and
throughput at 1, 6 and 24 requests in flight?

## Break it, then fix it

**Break 1: a replica dies.** The router scrapes metrics but has no health checks.

```bash
docker compose stop sim-2
for t in acme globex initech hooli; do chat $t prefix_aware 2>&1 | tail -1 | cut -c1-110; done
for i in 1 2 3; do chat umbrella round_robin 2>&1 | tail -1 | cut -c1-110; done
curl -s $ROUTER/stats | jq -c .load
```

**Predict (P7):** which requests fail, with what status? Will `acme` and `globex` keep failing?

**Fix:** bring it back and send `acme` again:

```bash
docker compose start sim-2 && sleep 2 && chat acme prefix_aware
```

**Predict (P8):** `acme` was a cache hit before. Is it now?

**Break 2: a KV cache too small for the batch.** Start a fourth simulator with 300 blocks (4,800 tokens) and send it
six 1.5k-token prompts at once:

```bash
docker compose run -d --name lab-tiny-cache sim-1 --model=fleet-8b --port=8000 --max-model-len=8192 --max-num-seqs=8 \
  --latency-calculator=per-token --prefill-overhead=20ms --prefill-time-per-token=200us --inter-token-latency=15ms \
  --enable-kvcache --kv-cache-size=300 --block-size=16
sleep 3
docker compose exec -T router python -c "
import asyncio, httpx
async def main():
    async with httpx.AsyncClient(timeout=60) as c:
        async def one(i):
            s = ' '.join(f't{i}w{j}' for j in range(1500))
            r = await c.post('http://lab-tiny-cache:8000/v1/chat/completions', json={'model': 'fleet-8b', 'max_tokens': 32,
                'messages': [{'role': 'system', 'content': s}, {'role': 'user', 'content': 'hi'}]})
            return r.status_code, r.text[:100]
        res = await asyncio.gather(*(one(i) for i in range(6)))
        print([s for s, _ in res]); print(next((t for s, t in res if s != 200), ''))
asyncio.run(main())"
```

**Predict (P9):** how many of the six succeed?

**Fix:** `docker rm -f lab-tiny-cache`. When you're done: `mise run down`.

## Check your predictions

**P1.** 2 (K and V) × 32 layers × 8 KV heads × 128 × 2 bytes = **131,072 bytes**, 128 KiB per token. About 451k
tokens fit after the weights (14.9 GiB) and runtime reserve, so **110** requests of 4k tokens. Grouped-query
attention (8 KV heads instead of 32) is why that number isn't four times smaller.

**P2.** **8** in BF16, **78** in FP8. The 32B model's BF16 weights (61 GiB) leave almost nothing for KV; FP8 halves
the weights *and* each token's KV, so the cache grows about ten times. Same GPU, roughly ten times the
concurrency, which is most of why FP8 dominates production serving.

**P3.**

```text
1  8.2 ms/step   122 tok/s  20.57 $/M
8  10.1 ms/step  792 tok/s   3.16 $/M
32 16.5 ms/step 1939 tok/s   1.29 $/M
64 25.1 ms/step 2554 tok/s   0.98 $/M
```

Each step reads all the weights once whatever the batch, so a step at batch 64 is only about 3× slower than at
batch 1 while producing 64 tokens: about **21× cheaper** per token. Per-user speed drops a little; cost collapses.
That's continuous batching in one table.

**P4.** First `acme`: **374 ms**, `cached_prompt_tokens=0`. Next two: **~50 ms**, `cached_prompt_tokens=1504`. The
1,500-token prefill is skipped entirely. `globex` hashed to the same replica here (`sim-2`) but is a different
prefix: 356 ms, 0 cached. Which replica a tenant hashes to is deterministic but arbitrary.

**P5.** The **fourth**: requests 1-3 go to `sim-1`, `sim-2`, `sim-3`, each a cold 350 ms prefill, and only when
round-robin comes back to `sim-1` is the prefix cached (47 ms). Round-robin pays the prefill once per replica per
tenant, and an agent session's growing history (which changes every turn) almost never hits.

**P6.**

```text
concurrency 1   TTFT p50  131 p95  145 ms | ITL 16.3 ms |  48 out tok/s
concurrency 6   TTFT p50  152 p95  498 ms | ITL 18.2 ms | 213 out tok/s
concurrency 24  TTFT p50 1080 p95 1756 ms | ITL 22.1 ms | 363 out tok/s
```

Up to the slot count, more concurrency buys throughput almost for free. Past it, requests queue: TTFT p50 jumps
about 8× while throughput grows less than 2×, and ITL creeps up as batches fill (`--time-factor-under-load`). The
knee sits between 6 and 24 here; you'd set the autoscaler to add a replica when `vllm:num_requests_waiting`
stays above a few, well before the knee.

**P7.** Every request routed to `sim-2` fails with **502** (`backend http://sim-2:8000 unavailable`): `acme` and
`globex` are pinned there by their prefix hash, and round-robin sends a third of its traffic there. The others
succeed. They keep failing: the router only notices a dead replica when a request to it fails, so prefix-aware
keeps choosing it. llm-d's endpoint picker and Kubernetes readiness probes remove dead pods from the pool; this
router has neither. (While building this lab, the first version of the router also leaked its in-flight counter on
these errors, which made the dead replica look busy forever. Error paths are where load balancers rot.)

**P8.** No: 357 ms, `cached_prompt_tokens=0`. The KV cache lives in the server process's GPU memory (here, the
simulator's RAM). A restart loses it, which is why rolling restarts of model servers briefly raise TTFT across the
fleet, and why llm-d offers KV-cache offloading and sharing.

**P9.** **3** of 6: `[200, 200, 200, 500, 500, 500]` with `the kv cache does not have sufficient capacity to store this
request`. Three 1.5k-token prompts fill 300 blocks. Real vLLM would queue or preempt instead of failing, but the
effect on users is similar: the KV cache, not the GPU's compute, caps concurrency. That's the number you size in
section 1.

## What to remember

- KV memory per token × context × concurrency must fit next to the weights. FP8 is the biggest lever.
- Batching makes tokens cheap; queues make them slow. Find the knee and scale before it.
- Prefix-aware routing turns a 350 ms prefill into a 50 ms cache hit; round-robin throws that away.
- Health checks and readiness gates are part of the load balancer, not optional extras.

Next: [Lab 05 · Batch AI on the lakehouse](05-batch-ai-lakehouse.md).
