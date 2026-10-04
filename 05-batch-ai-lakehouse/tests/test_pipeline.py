import numpy as np
import pytest

from lakehouse_ai import embed, enrich, llm
from lakehouse_ai.seed import tickets
from lakehouse_ai.tables import arrow, catalog, table


@pytest.fixture
def root(tmp_path):
    cat = catalog(str(tmp_path))
    t = table(cat, "tickets")
    t.append(arrow(t, tickets(200)))
    return str(tmp_path)


async def test_incremental_and_idempotent(root):
    first = await enrich.run("scripted", root=root)
    assert first["inserted"] == 200 and first["rows_cached"] > 0          # duplicates enriched once
    assert first["rows_called"] + first["rows_cached"] == 200
    again = await enrich.run("scripted", root=root)
    assert again["rows_considered"] == 0
    e = table(catalog(root), "ticket_enrichment").scan().to_arrow()
    assert e.num_rows == 200 and len(set(e["ticket_id"].to_pylist())) == 200


async def test_edited_ticket_is_reenriched_in_place(root):
    await enrich.run("scripted", root=root)
    t = table(catalog(root), "tickets")
    row = t.scan(row_filter="ticket_id == 'OPS-000005'").to_arrow().to_pylist()[0]
    row["text"] = "Production Airflow scheduler crashed, everything is down"
    t.upsert(arrow(t, [row]), join_cols=["ticket_id"])
    out = await enrich.run("scripted", root=root)
    assert (out["rows_considered"], out["updated"]) == (1, 1)
    got = table(catalog(root), "ticket_enrichment").scan(row_filter="ticket_id == 'OPS-000005'").to_arrow()
    assert got.num_rows == 1 and got["category"][0].as_py() == "outage"


async def test_invalid_model_output_goes_to_dead_letter_table(root, monkeypatch):
    async def broken(self, items):
        return {k: llm.Outcome(None, 10, 5, error="invalid enum", raw='{"category": "weather"}') for k in items}
    monkeypatch.setattr(llm.Scripted, "run", broken)
    out = await enrich.run("scripted", root=root)
    assert out["rows_failed"] == 200 and out["inserted"] == 0
    assert table(catalog(root), "enrichment_errors").scan().count() == 200


def test_validate_rejects_bad_enums_and_drops_fake_ids():
    with pytest.raises(ValueError):
        llm.validate({"category": "weather", "severity": "low"})
    ok = llm.validate({"category": "cost", "severity": "low", "instance_ids": ["i-0abc12345", "not-an-id"],
                       "summary": "x"})
    assert ok["instance_ids"] == ["i-0abc12345"]


def test_batch_pricing_is_half():
    assert llm.cost("claude-haiku-4-5", 1_000_000, 0, batch=True) == pytest.approx(0.5)


def test_embeddings_put_similar_tickets_closer():
    a, b, c = embed.embed(["disk full on i-1, compaction failing", "disk full on i-2, compaction failing",
                           "please add me to the IAM role"])
    assert float(np.dot(a, b)) > float(np.dot(a, c))
