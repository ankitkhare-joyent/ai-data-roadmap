"""Score triage against the expected outcome for each incident.

The action is weighted most: a correct diagnosis with the wrong action (patching a consumer for a producer's
contract break) is the failure that hurts in production.
"""
from __future__ import annotations

import argparse
import sys

from data_agent.triage import triage_all

EXPECTED = {
    "run-oom": {"category": "resources", "action": "patch", "owner": None, "dashboard": True},
    "run-drift": {"category": "schema_drift", "action": "issue", "owner": "cloud-inventory-oncall@acme.example",
                  "dashboard": True},
    "run-late": {"category": "upstream_late", "action": "rerun", "owner": None, "dashboard": True},
    "run-perm": {"category": "permissions", "action": "escalate", "owner": None, "dashboard": False},
}


def score(reports) -> tuple[float, list[str]]:
    lines, total = [], 0.0
    for r in reports:
        exp = EXPECTED[r.run_id]
        dash = any(j.startswith("superset://") for j in r.blast_radius.get("jobs", []))
        checks = {
            "action": (r.action == exp["action"], 0.5),
            "category": (r.category == exp["category"], 0.2),
            "blast radius": (dash == exp["dashboard"], 0.15),
            "owner": (exp["owner"] is None or r.owner_to_notify == exp["owner"], 0.15),
        }
        if exp["action"] == "patch":
            checks["patch passes checks"] = (bool(r.proposal and r.proposal["ok"]), 0.0)
        s = sum(w for ok, w in checks.values() if ok)
        total += s
        failed = [k for k, (ok, _) in checks.items() if not ok]
        lines.append(f"{r.run_id:<10} score {s:.2f}  {'all checks pass' if not failed else 'failed: ' + ', '.join(failed)}")
    return total / len(reports), lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", action="store_true")
    a = ap.parse_args()
    avg, lines = score(triage_all(a.claude))
    print("\n".join(lines))
    print(f"\nmean score {avg:.2f}")
    return 0 if avg >= 0.99 else 1


if __name__ == "__main__":
    sys.exit(main())
