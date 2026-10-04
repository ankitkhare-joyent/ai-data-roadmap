"""Score enrichment against human labels: per-field accuracy, a confusion list, and cost per correct label.

Run it on every prompt_version or model change before backfilling millions of rows. The number that
decides model choice is not accuracy alone but accuracy per dollar on YOUR tickets.
"""
from __future__ import annotations

from collections import Counter

from lakehouse_ai import llm
from lakehouse_ai.tables import catalog, table


def evaluate(model: str = "scripted", prompt_version: str = llm.PROMPT_VERSION, root: str | None = None) -> dict:
    cat = catalog(root)
    gold = {r["ticket_id"]: r for r in table(cat, "tickets").scan(
        row_filter="gold_category is not null",
        selected_fields=("ticket_id", "gold_category", "gold_severity")).to_arrow().to_pylist()}
    pred = table(cat, "ticket_enrichment").scan(
        row_filter=f"prompt_version == '{prompt_version}' and model == '{model}'",
        selected_fields=("ticket_id", "category", "severity")).to_arrow().to_pylist()
    pairs = [(gold[p["ticket_id"]], p) for p in pred if p["ticket_id"] in gold]
    n = len(pairs) or 1
    cat_ok = sum(g["gold_category"] == p["category"] for g, p in pairs)
    sev_ok = sum(g["gold_severity"] == p["severity"] for g, p in pairs)
    confusions = Counter((g["gold_category"], p["category"]) for g, p in pairs if g["gold_category"] != p["category"])
    runs = table(cat, "enrichment_runs").scan(
        row_filter=f"model == '{model}' and prompt_version == '{prompt_version}'",
        selected_fields=("est_cost_usd",)).to_arrow().to_pylist()
    spent = sum(r["est_cost_usd"] for r in runs)
    return {"model": model, "prompt_version": prompt_version, "labelled": len(pairs),
            "category_accuracy": round(cat_ok / n, 3), "severity_accuracy": round(sev_ok / n, 3),
            "top_confusions": confusions.most_common(3), "spent_usd": round(spent, 4),
            "usd_per_correct_category": round(spent / cat_ok, 6) if cat_ok else None}
