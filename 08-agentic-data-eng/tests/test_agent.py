import pytest

from data_agent import evals, patcher
from data_agent.platform import Lineage, validate_sample
from data_agent.triage import triage_all


def test_lineage_blast_radius_reaches_the_dashboard():
    d = Lineage().downstream("ec2.instances")
    assert "finance.ec2_daily_cost" in d["datasets"] and "superset://prod/fleet_cost_dashboard" in d["jobs"]


def test_contract_check_finds_rename_and_bad_values():
    r = validate_sample("ec2.instances_raw")
    assert r["missing_required"] == ["instanceType"] and r["unexpected_columns"] == ["instance_type"]
    assert r["invalid_values"] == {"state": ["stoped"]} and r["owner"].startswith("cloud-inventory")


def test_scripted_triage_scores_full_marks():
    assert evals.score(triage_all())[0] == pytest.approx(1.0)


def test_late_run_is_chained_to_the_failed_upstream():
    late = next(r for r in triage_all() if r.run_id == "run-late")
    assert any("run-oom" in n for n in late.notes)


@pytest.mark.parametrize("path,changes,problem", [
    ("dags/ec2_inventory.py", {"spark.executor.memory": "8g"}, None),
    ("dags/ec2_inventory.py", {"execution_role_arn": "arn:aws:iam::1:role/admin"}, "allow-list"),
    ("terraform/iam.tf", {"spark.executor.memory": "8g"}, "only change DAG files"),
])
def test_patcher_guardrails(path, changes, problem):
    p = patcher.propose("t", path, changes, ["evidence"], "test")
    if problem is None:
        assert p.ok and '+    "spark.executor.memory": "8g",' in p.diff and p.branch == "agent/t"
    else:
        assert not p.ok and any(problem in x for x in p.problems)


def test_patcher_never_touches_the_source_repo():
    before = (patcher.REPO / "dags/ec2_inventory.py").read_text()
    patcher.propose("t2", "dags/ec2_inventory.py", {"spark.executor.memory": "16g"}, [], "test")
    assert (patcher.REPO / "dags/ec2_inventory.py").read_text() == before
