"""Back-of-envelope sizing for self-hosted LLM serving: memory, concurrency, speed and cost.

    python -m serving_lab.sizing

The numbers are estimates to make decisions with (how many GPUs, which quantisation, self-host or API),
not promises. Always confirm with a benchmark on the real hardware (bench.py).

Three facts drive everything:
  1. Weights must fit:      params x bytes_per_param, split across tensor-parallel GPUs.
  2. The KV cache is the working memory for concurrent requests:
         bytes per token = 2 (K and V) x layers x kv_heads x head_dim x bytes_per_elem
     Whatever memory the weights leave is how many tokens of context you can hold at once.
  3. Decode is memory-bandwidth bound: every step reads all the weights plus the KV cache of the batch,
     so batching many requests together is what makes GPUs cheap per token.
"""
from __future__ import annotations

from dataclasses import dataclass

GIB = 1024**3


@dataclass(frozen=True)
class Model:
    name: str
    params_b: float
    layers: int
    kv_heads: int        # grouped-query attention: far fewer KV heads than query heads
    head_dim: int


@dataclass(frozen=True)
class Gpu:
    name: str
    mem_gib: float
    bandwidth_tbs: float  # HBM bandwidth, TB/s
    usd_per_hour: float   # on-demand list price ballpark; use your negotiated rate


MODELS = {
    "llama-3.1-8b": Model("Llama 3.1 8B", 8.0, 32, 8, 128),
    "qwen3-32b": Model("Qwen3 32B", 32.8, 64, 8, 128),
    "llama-3.1-70b": Model("Llama 3.1 70B", 70.6, 80, 8, 128),
}
GPUS = {
    "l40s": Gpu("L40S 48GB", 48, 0.86, 1.9),
    "a100": Gpu("A100 80GB", 80, 2.0, 3.0),
    "h100": Gpu("H100 80GB", 80, 3.35, 4.5),
}
BYTES = {"bf16": 2.0, "fp8": 1.0, "int4": 0.5}


@dataclass(frozen=True)
class Plan:
    model: Model
    gpu: Gpu
    tp: int                 # tensor-parallel GPUs per replica
    weights_dtype: str = "bf16"
    kv_dtype: str = "bf16"
    gpu_memory_utilization: float = 0.90   # vLLM's default share of GPU memory it may use
    activation_reserve_gib: float = 2.0    # per GPU: activations, CUDA graphs, fragmentation

    @property
    def weights_gib(self) -> float:
        return self.model.params_b * 1e9 * BYTES[self.weights_dtype] / GIB

    @property
    def kv_bytes_per_token(self) -> float:
        m = self.model
        return 2 * m.layers * m.kv_heads * m.head_dim * BYTES[self.kv_dtype]

    @property
    def kv_budget_gib(self) -> float:
        usable = self.tp * (self.gpu.mem_gib * self.gpu_memory_utilization - self.activation_reserve_gib)
        return usable - self.weights_gib

    @property
    def fits(self) -> bool:
        return self.kv_budget_gib > 0

    @property
    def kv_tokens(self) -> int:
        return max(0, int(self.kv_budget_gib * GIB / self.kv_bytes_per_token))

    def max_concurrency(self, avg_context_tokens: int) -> int:
        return self.kv_tokens // avg_context_tokens

    def decode_step_ms(self, batch: int, avg_context_tokens: int, efficiency: float = 0.6) -> float:
        """Memory-bandwidth model of one decode step: read all weights once plus every sequence's KV."""
        bytes_read = self.weights_gib * GIB + batch * avg_context_tokens * self.kv_bytes_per_token
        return bytes_read / (self.tp * self.gpu.bandwidth_tbs * 1e12 * efficiency) * 1e3

    def output_tokens_per_s(self, batch: int, avg_context_tokens: int) -> float:
        return batch / (self.decode_step_ms(batch, avg_context_tokens) / 1e3)

    @property
    def usd_per_hour(self) -> float:
        return self.tp * self.gpu.usd_per_hour


def usd_per_million_output_tokens(plan: Plan, batch: int, ctx: int, utilisation: float = 0.5) -> float:
    """Self-hosted cost per million output tokens, assuming the replica is busy `utilisation` of the time."""
    tokens_per_hour = plan.output_tokens_per_s(batch, ctx) * 3600 * utilisation
    return plan.usd_per_hour / tokens_per_hour * 1e6


def report(plan: Plan, ctx: int = 4096) -> str:
    if not plan.fits:
        return (f"{plan.model.name:<14} {plan.weights_dtype:<5} on {plan.tp}x {plan.gpu.name:<10} "
                f"does not fit: weights {plan.weights_gib:.0f} GiB")
    batch = max(1, min(plan.max_concurrency(ctx), 64))
    return (f"{plan.model.name:<14} {plan.weights_dtype:<5} on {plan.tp}x {plan.gpu.name:<10} "
            f"weights {plan.weights_gib:5.1f} GiB | KV {plan.kv_bytes_per_token / 1024:4.0f} KiB/token, "
            f"{plan.kv_tokens / 1000:6.0f}k tokens = {plan.max_concurrency(ctx):4d} x {ctx // 1024}k ctx | "
            f"batch {batch:2d}: ITL {plan.decode_step_ms(batch, ctx):5.1f} ms, "
            f"{plan.output_tokens_per_s(batch, ctx):6.0f} tok/s | "
            f"${usd_per_million_output_tokens(plan, batch, ctx):5.2f}/M out @50% busy")


def main() -> None:
    print("Sizing at 4k tokens of context per request (prompt + output). Estimates: benchmark to confirm.\n")
    plans = [
        Plan(MODELS["llama-3.1-8b"], GPUS["l40s"], 1),
        Plan(MODELS["llama-3.1-8b"], GPUS["h100"], 1),
        Plan(MODELS["qwen3-32b"], GPUS["l40s"], 1),
        Plan(MODELS["qwen3-32b"], GPUS["h100"], 1),
        Plan(MODELS["qwen3-32b"], GPUS["h100"], 1, weights_dtype="fp8", kv_dtype="fp8"),
        Plan(MODELS["llama-3.1-70b"], GPUS["h100"], 1),
        Plan(MODELS["llama-3.1-70b"], GPUS["h100"], 2, weights_dtype="fp8", kv_dtype="fp8"),
        Plan(MODELS["llama-3.1-70b"], GPUS["h100"], 4),
    ]
    for p in plans:
        print(report(p))
    print("\nCompare: Claude Haiku 4.5 is $5/M output tokens, Sonnet 5.5 $10/M, Opus 5.5 $20/M, with no idle "
          "cost, no ops team and much stronger models. Self-hosting wins on high, steady volume of a narrow "
          "task, on data that cannot leave your network, or on latency you control; rarely on price alone.")


if __name__ == "__main__":
    main()
