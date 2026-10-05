"""Triage failed pipeline runs: classify, find the blast radius, check contracts, pick the right action.

    python -m data_agent.triage [--claude]

The action matters more than the diagnosis. An agent that "fixes" every failure by patching the consumer
does damage: a schema change upstream is a broken contract and belongs to the producer; a late upstream
needs a rerun, not code; an AccessDenied needs an IAM review by a human. Only resource tuning is safe to
propose as a patch, and even that goes through review.

  patch      open a PR with an allow-listed change (patcher.py)
  rerun      no code change; clear and rerun after the upstream lands
  issue      file it against the owning team named in the data contract; quarantine the bad data
  escalate   a human with the right permissions decides (IAM, secrets, infra)
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field

from data_agent import patcher
from data_agent.platform import Lineage, contract, task_log, validate_sample


@dataclass
class Report:
    run_id: str
    job: str
    category: str                  # resources | schema_drift | upstream_late | permissions | unknown
    action: str                    # patch | rerun | issue | escalate
    root_cause: str
    evidence: list[str]
    blast_radius: dict
    owner_to_notify: str | None = None
    proposal: dict | None = None
    notes: list[str] = field(default_factory=list)


def triage_run(run, lin: Lineage) -> Report:
    log = task_log(run.run_id)
    text = f"{run.error}\n{log}"
    inputs, outputs = lin.job_io(run.job)
    radius = {"outputs": outputs, **lin.downstream(outputs[0])} if outputs else {}
    ev = [line.split("} ", 1)[-1][:180] for line in log.splitlines() if re.search(r"ERROR|WARN", line)][:4]

    if re.search(r"OOMKilled|exceeding memory|OutOfMemoryError", text):
        skew = re.search(r"input size ([\d.]+ GiB), median task input ([\d.]+ MiB)", text)
        changes = {"spark.executor.memory": "8g", "spark.executor.memoryOverhead": "2g"}
        notes = []
        if skew:
            changes["spark.sql.adaptive.skewJoin.enabled"] = "true"
            notes.append(f"one task read {skew.group(1)} against a {skew.group(2)} median: data skew, "
                         "so memory alone may only delay the next failure")
        prop = patcher.propose(run.run_id, "dags/ec2_inventory.py", changes, ev,
                               f"Raise executor memory for {run.job} after OOM")
        return Report(run.run_id, run.job, "resources", "patch",
                      "Spark executors were OOM-killed on a skewed partition", ev, radius,
                      proposal=asdict(prop), notes=notes)

    if m := re.search(r"UNRESOLVED_COLUMN.*?`(\w+)`.*?Did you mean.*?`(\w+)`", text, re.DOTALL):
        missing, suggested = m.group(1), m.group(2)
        src = inputs[0] if inputs else ""
        check = validate_sample(src)
        c = contract(src) or {}
        notes = [(f"contract {c.get('name')} v{check.get('contract_version')} requires `{missing}`; the latest data "
                  f"has `{suggested}` instead: the producer changed the schema without a new contract version")]
        if check.get("invalid_values"):
            notes.append(f"also invalid values against the contract: {check['invalid_values']}")
        return Report(run.run_id, run.job, "schema_drift", "issue",
                      f"Upstream renamed `{missing}` to `{suggested}` in {src}", ev,
                      {"outputs": outputs, **lin.downstream(src)}, owner_to_notify=check.get("owner"), notes=notes)

    if re.search(r"SensorTimeout|Sensor .* timed out", text):
        upstream_jobs = sorted({j.rsplit("/", 1)[1] for d in inputs for j in lin.producer_of.get(d, ())})
        last = {j: lin.last_success(j) for j in upstream_jobs}
        late = [j for j, t in last.items() if t and t < run.time[:10]]
        notes = [f"last successful runs: {last}"]
        # Chain to the real root cause: did the upstream itself fail today?
        failed_up = [r.run_id for r in lin.runs("FAIL") if r.job in upstream_jobs and r.time[:10] == run.time[:10]]
        if failed_up:
            notes.append(f"the upstream failed today in {failed_up}: fix that first, this run will follow")
        notes.append("rerun after the upstream completes; no code change")
        return Report(run.run_id, run.job, "upstream_late", "rerun",
                      f"Upstream {late or upstream_jobs} had not produced today's data", ev, radius, notes=notes)

    if re.search(r"AccessDenied|not authorized to perform", text):
        perm = re.search(r"perform: ([\w:]+) on resource:? (\S+)", text)
        return Report(run.run_id, run.job, "permissions", "escalate",
                      f"Role lacks {perm.group(1)} on {perm.group(2)}" if perm else "Access denied", ev, radius,
                      owner_to_notify="platform-iam@acme.example",
                      notes=["the agent never edits IAM; suggest the minimal policy statement in the ticket"])

    return Report(run.run_id, run.job, "unknown", "escalate", "Could not classify", ev, radius)


def triage_all(use_claude: bool = False) -> list[Report]:
    lin = Lineage()
    failed = lin.runs("FAIL")
    if use_claude:
        from data_agent.claude_agent import triage_with_claude
        return [triage_with_claude(r, lin) for r in failed]
    return [triage_run(r, lin) for r in failed]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    for r in triage_all(a.claude):
        if a.json:
            print(json.dumps(asdict(r), indent=2))
            continue
        print(f"\n== {r.run_id}: {r.job}")
        print(f"   {r.category} -> {r.action.upper()}: {r.root_cause}")
        dash = [j for j in r.blast_radius.get("jobs", []) if j.startswith("superset://")]
        print(f"   blast radius: {len(r.blast_radius.get('datasets', []))} downstream datasets, "
              f"{len(r.blast_radius.get('jobs', []))} jobs" + (f", dashboards: {dash}" if dash else ""))
        if r.owner_to_notify:
            print(f"   notify: {r.owner_to_notify}")
        for n in r.notes:
            print(f"   note: {n}")
        if r.proposal:
            p = r.proposal
            print(f"   proposal on branch {p['branch']} ({'checks passed' if p['ok'] else p['problems']}):")
            print("\n".join("      " + line for line in p["diff"].splitlines() if line[:1] in "+-"))


if __name__ == "__main__":
    main()
