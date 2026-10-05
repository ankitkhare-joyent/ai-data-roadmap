"""Context engineering, measured: one long agent session under four strategies, with prompt caching modelled.

    python -m agent_memory.context

The context window is working memory (RAM), not storage. A 40-turn agent that reads a 3k-token tool result
every turn would resend 120k+ tokens of mostly stale results on every call. The strategies:

  none       keep everything (the default most demos ship)
  clear      clear old tool results once the context passes a trigger, keeping the last few; this is what
             the API's `clear_tool_uses_20250919` context edit does server-side
  offload    write each big tool result to the memory store and keep only a short preview plus the path
             in context; the agent re-reads a file when it needs one (just-in-time retrieval)
  compact    when the context passes a threshold, replace the history with a summary; what server-side
             compaction (`compact_20260112`) does

Caching changes the economics. Every request re-sends the prompt, but an unchanged prefix is billed as a
cache read at 10% of the input price, and new tokens as a cache write at 125%. Any edit to earlier content
(clearing, compaction) invalidates the cache from that point on. So a strategy that saves tokens can
still cost more if it breaks the cache too often, which is why `clear_tool_uses` has a `clear_at_least`
setting: only clear when it frees enough to pay for rebuilding the cache.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

PRICE_IN, PRICE_OUT = 4e-6, 20e-6          # Claude Opus 5.5, USD per token
CACHE_READ, CACHE_WRITE = 0.1, 1.25         # multipliers on the input price


@dataclass(frozen=True)
class Seg:
    key: str          # identity for cache matching: same key = byte-identical content
    tokens: int
    kind: str         # prefix | user | assistant | tool | summary


@dataclass
class Session:
    turns: int = 40
    prefix: int = 4000          # system prompt + tool definitions
    user: int = 100
    assistant: int = 300        # reasoning text + tool call
    tool_result: int = 3000
    reread_rate: float = 0.15   # how often the agent needs an older tool result again
    seed: int = 3
    rng: random.Random = field(init=False)

    def __post_init__(self):
        self.rng = random.Random(self.seed)


@dataclass
class Stats:
    name: str
    peak_context: int = 0
    input_tokens: int = 0       # what the model processed in total
    billed_input_usd: float = 0.0
    output_usd: float = 0.0
    cache_breaks: int = 0
    rereads: int = 0
    lost_results: int = 0       # results neither in context nor recoverable


def prefix_match(prev: list[Seg], cur: list[Seg]) -> int:
    n = 0
    for a, b in zip(prev, cur, strict=False):
        if a.key != b.key:
            break
        n += a.tokens
    return n


def simulate(strategy: str, s: Session, trigger: int = 40_000, keep: int = 3, clear_at_least: int = 15_000,
             compact_at: int = 60_000, summary: int = 2_000, preview: int = 150) -> Stats:
    st = Stats(strategy)
    ctx: list[Seg] = [Seg("prefix", s.prefix, "prefix")]
    prev: list[Seg] = []
    stored: set[int] = set()        # tool results available in the memory store (offload)

    def send():
        total = sum(x.tokens for x in ctx)
        hit = prefix_match(prev, ctx)
        if prev and hit < sum(x.tokens for x in prev) - s.assistant:   # an earlier segment changed
            st.cache_breaks += 1
        st.peak_context = max(st.peak_context, total)
        st.input_tokens += total
        st.billed_input_usd += (hit * CACHE_READ + (total - hit) * CACHE_WRITE) * PRICE_IN
        st.output_usd += s.assistant * PRICE_OUT
        return list(ctx)

    for t in range(s.turns):
        ctx.append(Seg(f"user{t}", s.user, "user"))
        if strategy == "offload" and t > 2 and s.rng.random() < s.reread_rate:
            # the agent re-reads an older result from memory: one extra tool round trip
            st.rereads += 1
            prev = send()
            ctx += [Seg(f"reread-call{t}", 60, "assistant"), Seg(f"reread{t}", s.tool_result, "tool")]
        prev = send()
        ctx.append(Seg(f"asst{t}", s.assistant, "assistant"))
        if strategy == "offload":
            stored.add(t)
            ctx.append(Seg(f"preview{t}", preview, "tool"))
        else:
            ctx.append(Seg(f"tool{t}", s.tool_result, "tool"))

        total = sum(x.tokens for x in ctx)
        if strategy == "clear" and total > trigger:
            tools = [i for i, x in enumerate(ctx) if x.kind == "tool" and not x.key.startswith("cleared")]
            victims = tools[:-keep]
            freed = sum(ctx[i].tokens - 20 for i in victims)
            if freed >= clear_at_least:
                for i in victims:
                    ctx[i] = Seg(f"cleared-{ctx[i].key}", 20, "tool")   # "[tool result cleared]" placeholder
                    st.lost_results += 1
        if strategy == "compact" and total > compact_at:
            dropped = sum(1 for x in ctx if x.kind == "tool")
            ctx = [ctx[0], Seg(f"summary{t}", summary, "summary")]
            st.lost_results += dropped        # details survive only as far as the summary kept them
    if strategy == "clear":
        st.lost_results = sum(1 for x in ctx if x.key.startswith("cleared"))
    return st


def main() -> None:
    s = Session()
    print(f"A {s.turns}-turn agent session: {s.prefix // 1000}k-token system prompt + tools, a "
          f"{s.tool_result // 1000}k-token tool result per turn. Prices: Claude Opus 5.5 with prompt caching.\n")
    print(f"{'strategy':<9} {'peak ctx':>9} {'tokens processed':>17} {'input $':>8} {'output $':>9} "
          f"{'cache breaks':>13} {'re-reads':>9} {'results not in context':>23}")
    for name in ("none", "clear", "offload", "compact"):
        r = simulate(name, Session())
        print(f"{name:<9} {r.peak_context:>9,} {r.input_tokens:>17,} {r.billed_input_usd:>8.2f} "
              f"{r.output_usd:>9.2f} {r.cache_breaks:>13} {r.rereads:>9} {r.lost_results:>23}")
    print("\nRead it like this: caching already makes 'none' cheaper than its token count suggests, but its "
          "context keeps growing until it hits the window and quality drops long before that ('context rot'). "
          "'offload' keeps the context small and the cache intact, and nothing is lost because results live "
          "in memory. 'clear' and 'compact' bound the context but break the cache and drop detail. That's "
          "why long-running agents pair memory with clearing or compaction, not one or the other.")


if __name__ == "__main__":
    main()
