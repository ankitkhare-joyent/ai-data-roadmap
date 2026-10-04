"""Synthetic ops tickets about the EC2 fleet, with gold labels on every row (so evals are exact)."""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

TEMPLATES: dict[tuple[str, str], list[str]] = {
    ("outage", "critical"): [
        "{iid} in {acct} is unreachable and the Trino coordinator on it is down, dashboards failing",
        "Production Airflow scheduler on {iid} crashed, no DAGs running since {t}",
    ],
    ("capacity", "high"): [
        "Spark executors on {iid} keep getting OOM killed, nightly job late again",
        "Disk on {iid} at 97%, Iceberg compaction failing with no space left on device",
    ],
    ("capacity", "medium"): [
        "CPU on {iid} averaging 85% during business hours, consider a bigger instance",
    ],
    ("cost", "low"): [
        "{iid} has been idle for 3 weeks (p95 CPU 2%), can we stop it?",
        "Dev box {iid} left running over the weekend again",
    ],
    ("security", "high"): [
        "Security group on {iid} allows 0.0.0.0/0 on port 22, please lock down",
        "IMDSv1 still enabled on {iid}, needs IMDSv2 enforcement",
    ],
    ("access", "medium"): [
        "Need SSM session access to {iid} for debugging the ingest job",
        "Please add me to the IAM role used by {iid}, getting AccessDenied on S3",
    ],
}
ACCOUNTS = ["392910111111", "392910222222", "392910333333"]


def instance_id(rng: random.Random) -> str:
    return "i-0" + "".join(rng.choice("0123456789abcdef") for _ in range(16))


def tickets(n: int, start_id: int = 1, seed: int = 42, start: datetime | None = None) -> list[dict]:
    rng = random.Random(seed)
    start = start or datetime(2026, 9, 1, tzinfo=UTC)
    keys = list(TEMPLATES)
    rows = []
    for i in range(n):
        cat, sev = rng.choice(keys)
        iid = instance_id(rng)
        text = rng.choice(TEMPLATES[(cat, sev)]).format(iid=iid, acct=rng.choice(ACCOUNTS), t="02:00 UTC")
        if rng.random() < 0.08 and rows:          # duplicate reports of the same problem happen a lot
            prev = rng.choice(rows)
            text, iid, cat, sev = prev["text"], prev["instance_id"], prev["gold_category"], prev["gold_severity"]
        rows.append({
            "ticket_id": f"OPS-{start_id + i:06d}",
            "created_at": start + timedelta(minutes=17 * i),
            "account": rng.choice(ACCOUNTS),
            "instance_id": iid,
            "text": text,
            "gold_category": cat,
            "gold_severity": sev,
        })
    return rows
