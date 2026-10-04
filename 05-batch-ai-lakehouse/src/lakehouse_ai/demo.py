"""The whole lesson in one run, offline: seed, enrich, add data, enrich again, evaluate, embed, search."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from lakehouse_ai import embed, enrich, evaluate, llm
from lakehouse_ai.seed import tickets
from lakehouse_ai.tables import arrow, catalog, table


def show(title: str, d: dict) -> None:
    keep = ("rows_considered", "rows_called", "rows_cached", "rows_failed", "inserted", "updated",
            "input_tokens", "est_cost_usd", "tickets_snapshot_id")
    print(f"\n== {title}\n   " + ", ".join(f"{k}={d[k]}" for k in keep if k in d))


async def main() -> None:
    cat = catalog()
    t = table(cat, "tickets")
    if t.current_snapshot() is None:
        t.append(arrow(t, tickets(2000)))
    print(f"ops.tickets: {t.scan().count()} rows, snapshot {t.current_snapshot().snapshot_id}")

    show("1. first enrichment run (backfill)", await enrich.run("scripted"))
    show("2. re-run with no new data: nothing to do, nothing paid", await enrich.run("scripted"))

    t.append(arrow(t, tickets(300, start_id=2001, seed=7, start=datetime(2026, 10, 1, tzinfo=UTC))))
    show("3. 300 new tickets arrived: only those are enriched", await enrich.run("scripted"))

    # A ticket edited after enrichment gets re-enriched because its text hash changed.
    edited = t.scan(row_filter="ticket_id == 'OPS-000001'").to_arrow().to_pylist()[0]
    edited["text"] += " UPDATE: now also failing health checks, production impact"
    t.upsert(arrow(t, [edited]), join_cols=["ticket_id"])
    show("4. one ticket edited: re-enriched and UPSERTed in place", await enrich.run("scripted"))

    print("\n== 5. evaluation against human labels")
    print("  ", evaluate.evaluate("scripted"))
    print("\n== labels by category:", enrich.label_counts())

    runs = table(cat, "enrichment_runs").scan(selected_fields=("input_tokens", "rows_called")).to_arrow()
    tin, calls = sum(runs["input_tokens"].to_pylist()), sum(runs["rows_called"].to_pylist())
    print(f"\n== what these {calls} calls would cost with Claude via the Batch API (50% off), at ~120 output tokens each:")
    for m in ("claude-haiku-4-5", "claude-sonnet-5-5", "claude-opus-5-5"):
        print(f"   {m:<18} ${llm.cost(m, tin, calls * 120, batch=True):6.2f}")

    print(f"\n== 6. embeddings into Lance: {embed.build()} vectors, IVF_PQ index")
    for hit in embed.search("disk full, compaction failing", k=3):
        print(f"   {hit['ticket_id']}  distance {hit['_distance']:.3f}  {hit['text'][:70]}")

    snaps = [s.snapshot_id for s in table(cat, "ticket_enrichment").snapshots()]
    print(f"\n== 7. time travel: ops.ticket_enrichment has {len(snaps)} snapshots; "
          f"the first one had {table(cat, 'ticket_enrichment').scan(snapshot_id=snaps[0]).count()} rows")


if __name__ == "__main__":
    asyncio.run(main())
