"""What the agent can see: OpenLineage events, Airflow task logs, ODCS data contracts, a sample of data.

In production these come from Marquez (or DataHub / your catalog's OpenLineage endpoint), the Airflow REST
API, a contracts repo, and Trino. Here they are files, so the lesson runs offline; the function names are
the ones you'd expose as MCP tools.
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

import yaml

DATA = Path(__file__).resolve().parents[2] / "data"


def _key(d: dict) -> str:
    return d["name"] if d["namespace"].startswith("iceberg://") else f"{d['namespace']}/{d['name']}"


@dataclass
class Run:
    run_id: str
    job: str
    state: str
    time: str
    error: str
    dag: str | None
    task: str | None


class Lineage:
    """A dataset/job graph built from OpenLineage run events (the open standard Airflow, Spark, dbt and
    Flink all emit). Edges: input dataset -> job -> output dataset."""

    def __init__(self, path: Path = DATA / "lineage" / "events.jsonl"):
        self.events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        self.consumers: dict[str, set[str]] = defaultdict(set)    # dataset -> jobs reading it
        self.produces: dict[str, set[str]] = defaultdict(set)     # job -> datasets written
        self.producer_of: dict[str, set[str]] = defaultdict(set)  # dataset -> jobs writing it
        self.schema: dict[str, list[str]] = {}
        for e in self.events:
            job = f"{e['job']['namespace']}/{e['job']['name']}"
            for i in e.get("inputs", []):
                self.consumers[_key(i)].add(job)
            for o in e.get("outputs", []):
                self.produces[job].add(_key(o))
                self.producer_of[_key(o)].add(job)
                if f := o.get("facets", {}).get("schema"):
                    self.schema[_key(o)] = [x["name"] for x in f["fields"]]   # latest wins

    def runs(self, state: str | None = None) -> list[Run]:
        out = []
        for e in self.events:
            f = e["run"].get("facets", {})
            af = f.get("airflow", {})
            out.append(Run(e["run"]["runId"], e["job"]["name"], e["eventType"], e["eventTime"],
                           f.get("errorMessage", {}).get("message", ""), af.get("dag", {}).get("dag_id"),
                           af.get("task", {}).get("task_id")))
        return [r for r in out if state is None or r.state == state]

    def last_success(self, job_name: str) -> str | None:
        times = [r.time for r in self.runs("COMPLETE") if r.job == job_name]
        return max(times) if times else None

    def job_io(self, job_name: str) -> tuple[list[str], list[str]]:
        for e in reversed(self.events):
            if e["job"]["name"] == job_name:
                return [_key(i) for i in e.get("inputs", [])], [_key(o) for o in e.get("outputs", [])]
        return [], []

    def downstream(self, dataset: str) -> dict[str, list[str]]:
        """Blast radius: every job and dataset reachable from `dataset`, breadth first."""
        jobs, datasets, seen = [], [], {dataset}
        q = deque([dataset])
        while q:
            d = q.popleft()
            for j in sorted(self.consumers.get(d, ())):
                if j not in jobs:
                    jobs.append(j)
                for o in sorted(self.produces.get(j, ())):
                    if o not in seen:
                        seen.add(o)
                        datasets.append(o)
                        q.append(o)
        return {"jobs": jobs, "datasets": datasets}


def task_log(run_id: str, max_lines: int = 40) -> str:
    p = DATA / "logs" / f"{run_id}.log"
    return "\n".join(p.read_text().splitlines()[-max_lines:]) if p.exists() else f"no log for {run_id}"


def contract(dataset: str) -> dict | None:
    p = DATA / "contracts" / f"{dataset}.odcs.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else None


def validate_sample(dataset: str) -> dict:
    """Check the latest sample of a dataset against its ODCS contract: required columns and validValues."""
    c = contract(dataset)
    p = DATA / "samples" / f"{dataset}.latest.json"
    if c is None or not p.exists():
        return {"dataset": dataset, "checked": False}
    rows = json.loads(p.read_text())
    props = c["schema"][0]["properties"]
    columns = set().union(*(r.keys() for r in rows))
    missing = [x["name"] for x in props if x.get("required") and x["name"] not in columns]
    declared = {x["name"] for x in props}
    unexpected = sorted(columns - declared)
    invalid = {}
    for x in props:
        for q in x.get("quality", []):
            valid = q.get("arguments", {}).get("validValues")
            if q.get("metric") == "invalidValues" and valid:
                bad = sorted({r.get(x["name"]) for r in rows if r.get(x["name"]) not in valid})
                if len(bad) > q.get("mustBeLessOrEqualTo", 0):
                    invalid[x["name"]] = bad
    owner = next((m["username"] for m in c.get("team", {}).get("members", []) if m.get("role") == "owner"), None)
    return {"dataset": dataset, "checked": True, "contract_version": c["version"], "owner": owner,
            "missing_required": missing, "unexpected_columns": unexpected, "invalid_values": invalid,
            "ok": not (missing or invalid)}
