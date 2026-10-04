import asyncio
import json

import httpx
import pytest

from serving_lab import bench, router
from serving_lab.sizing import GPUS, MODELS, Plan


def test_kv_cache_math_matches_hand_calculation():
    p = Plan(MODELS["llama-3.1-8b"], GPUS["h100"], 1)
    assert p.kv_bytes_per_token == 2 * 32 * 8 * 128 * 2 == 128 * 1024
    assert not Plan(MODELS["llama-3.1-70b"], GPUS["h100"], 1).fits
    fp8 = Plan(MODELS["qwen3-32b"], GPUS["h100"], 1, weights_dtype="fp8", kv_dtype="fp8")
    assert fp8.max_concurrency(4096) > 5 * Plan(MODELS["qwen3-32b"], GPUS["h100"], 1).max_concurrency(4096)


def test_bigger_batches_are_cheaper_per_token():
    p = Plan(MODELS["llama-3.1-8b"], GPUS["h100"], 1)
    assert p.output_tokens_per_s(32, 4096) > 10 * p.output_tokens_per_s(1, 4096)


def test_prefix_aware_is_sticky_until_the_preferred_replica_is_busy():
    pool = router.Pool(["http://a", "http://b", "http://c"])
    first = pool.pick("prefix_aware", "tenant-1 system prompt")
    assert all(pool.pick("prefix_aware", "tenant-1 system prompt") == first for _ in range(10))
    pool.inflight[first] = router.SPILL_SLACK + 5
    assert pool.pick("prefix_aware", "tenant-1 system prompt") != first


def test_workload_shares_system_prompts_per_tenant():
    s = bench.Workload(tenants=2, turns=3).sessions(4)
    assert s[0][0][0] == s[2][0][0] and s[0][0][0] != s[1][0][0]
    assert len(s[0][2]) == 1 + 3 + 2          # system + 3 user + 2 assistant messages by turn 3


async def test_bench_measures_ttft_and_itl_from_a_stream():
    async def stream():
        await asyncio.sleep(0.05)
        for i in range(5):
            yield f'data: {json.dumps({"choices": [{"delta": {"content": f"t{i}"}}]})}\n\n'.encode()
            await asyncio.sleep(0.01)
        yield f'data: {json.dumps({"choices": [], "usage": {"completion_tokens": 5}})}\n\n'.encode()
        yield b"data: [DONE]\n\n"

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=stream()))
    async with httpx.AsyncClient(transport=transport) as c:
        r = await bench.one(c, "http://x", "m", [{"role": "user", "content": "hi"}], 5)
    assert r.ok and r.out_tokens == 5 and len(r.itls) == 4
    assert r.ttft == pytest.approx(0.05, abs=0.04)
