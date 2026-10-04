"""Incremental, idempotent LLM enrichment of an Iceberg table.

    python -m lakehouse_ai.enrich --llm scripted|claude-batch|claude-online [--model claude-haiku-4-5]

The production rules this job follows (they are the same rules you apply to any Spark job, plus two new ones):
  1. Incremental: only rows with no enrichment for (prompt_version, model), or whose text changed since.
  2. Idempotent: results are UPSERTed on (ticket_id, prompt_version), so a re-run after a crash, or two
     overlapping runs, never duplicate rows.
  3. Reproducible: every output row carries prompt_version, model and run_id; every run records the
     tickets snapshot id it read. You can always answer "which prompt produced this label, from which data?"
  4. Dedupe before you pay: identical texts are enriched once (content hash), the rest reuse the result.
  5. Bad output is data, not a crash: schema-invalid answers land in ops.enrichment_errors.
  6. Cost is a first-class output: tokens and estimated $ per run in ops.enrichment_runs.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import uuid
from datetime import UTC, datetime

import pyarrow.compute as pc

from lakehouse_ai import llm
from lakehouse_ai.tables import arrow, catalog, table


def text_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()[:32]


async def run(mode: str = "scripted", model: str | None = None, limit: int | None = None,
              root: str | None = None) -> dict:
    cat = catalog(root)
    tickets, enrichment = table(cat, "tickets"), table(cat, "ticket_enrichment")
    backend = llm.backend(mode, model)
    model_name = backend.model
    run_id, started = uuid.uuid4().hex[:12], datetime.now(UTC)

    # Pin the snapshot we read, so the run is reproducible even while new tickets keep arriving.
    snapshot = tickets.current_snapshot()
    src = tickets.scan(snapshot_id=snapshot.snapshot_id if snapshot else None,
                       selected_fields=("ticket_id", "text")).to_arrow()

    done = enrichment.scan(
        row_filter=f"prompt_version == '{llm.PROMPT_VERSION}' and model == '{model_name}'",
        selected_fields=("ticket_id", "text_hash", "category", "severity", "instance_ids", "summary"),
    ).to_arrow().to_pylist()
    done_hash = {r["ticket_id"]: r["text_hash"] for r in done}
    by_hash = {r["text_hash"]: r for r in done}

    todo = [r for r in src.to_pylist() if done_hash.get(r["ticket_id"]) != text_hash(r["text"])]
    if limit:
        todo = todo[:limit]

    # Dedupe: one call per distinct text that we have never enriched under this prompt and model.
    need: dict[str, str] = {}
    for r in todo:
        h = text_hash(r["text"])
        if h not in by_hash:
            need.setdefault(h, r["text"])
    outcomes = await backend.run(need) if need else {}

    now = datetime.now(UTC)
    rows, errors, from_cache = [], [], 0
    in_tok = sum(o.input_tokens for o in outcomes.values())
    out_tok = sum(o.output_tokens for o in outcomes.values())
    for r in todo:
        h = text_hash(r["text"])
        cached = by_hash.get(h)
        o = outcomes.get(h)
        if cached is None and (o is None or o.result is None):
            errors.append({"run_id": run_id, "ticket_id": r["ticket_id"], "error": o.error if o else "missing",
                           "raw": o.raw if o else None})
            continue
        if cached is None:
            res = o.result
        else:
            res = {k: cached[k] for k in ("category", "severity", "instance_ids", "summary")}
            from_cache += 1
        rows.append({"ticket_id": r["ticket_id"], "prompt_version": llm.PROMPT_VERSION, "model": model_name,
                     "text_hash": h, **res,
                     "input_tokens": o.input_tokens if cached is None else 0,
                     "output_tokens": o.output_tokens if cached is None else 0,
                     "run_id": run_id, "enriched_at": now})
        if cached is None:
            by_hash[h] = {**res, "text_hash": h}     # later duplicates in this run reuse it

    upserted = enrichment.upsert(arrow(enrichment, rows), join_cols=["ticket_id", "prompt_version"]) if rows else None
    if errors:
        err_t = table(cat, "enrichment_errors")
        err_t.append(arrow(err_t, errors))
    summary = {
        "run_id": run_id, "started_at": started, "finished_at": datetime.now(UTC), "llm_mode": mode,
        "model": model_name, "prompt_version": llm.PROMPT_VERSION,
        "tickets_snapshot_id": snapshot.snapshot_id if snapshot else None,
        "rows_considered": len(todo), "rows_cached": from_cache,
        "rows_called": len(need), "rows_failed": len(errors), "input_tokens": in_tok, "output_tokens": out_tok,
        "est_cost_usd": llm.cost(model_name, in_tok, out_tok, batch=mode == "claude-batch"),
    }
    runs_t = table(cat, "enrichment_runs")
    runs_t.append(arrow(runs_t, [summary]))
    summary["inserted"] = upserted.rows_inserted if upserted else 0
    summary["updated"] = upserted.rows_updated if upserted else 0
    return summary


def label_counts(root: str | None = None) -> list[tuple[str, int]]:
    t = table(catalog(root), "ticket_enrichment").scan(selected_fields=("category",)).to_arrow()
    vc = pc.value_counts(t["category"]).to_pylist()
    return sorted(((d["values"], d["counts"]) for d in vc), key=lambda x: -x[1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default="scripted", choices=["scripted", "claude-batch", "claude-online"])
    ap.add_argument("--model")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    print(asyncio.run(run(a.llm, a.model, a.limit)))


if __name__ == "__main__":
    main()
