"""Proposing a fix: a branch, a diff, checks, and a PR description. Never a merge, never prod.

An agent that operates your platform should have exactly the power of a junior engineer on their first
week: it can open a pull request, and a human reviews it. Everything below enforces that in code:

  sandbox      work happens in a throwaway clone on a branch named agent/<run-id>
  allow-list   only DAG files may change, and only Spark tuning keys inside them; IAM roles, schedules and
               entry points are off limits however convincing the reasoning
  checks       the patched file must compile, and the diff must stay small
  output       a unified diff and a PR body with evidence; pushing and merging stay with humans and CI
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2] / "repo"
ALLOWED_FILES = re.compile(r"^dags/[a-z0-9_]+\.py$")
ALLOWED_SPARK_KEYS = {
    "spark.executor.memory", "spark.executor.memoryOverhead", "spark.executor.instances",
    "spark.sql.shuffle.partitions", "spark.sql.adaptive.enabled", "spark.sql.adaptive.skewJoin.enabled",
    "spark.sql.adaptive.advisoryPartitionSizeInBytes",
}
MAX_CHANGED_LINES = 12


@dataclass
class Proposal:
    ok: bool
    branch: str
    diff: str
    problems: list[str]
    pr_body: str


def _git(cwd: Path, *args: str) -> str:
    env_args = ["-c", "user.name=data-agent", "-c", "user.email=data-agent@acme.example"]
    return subprocess.run(["git", *env_args, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _spark_conf(source: str) -> dict[str, str]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "SPARK_CONF" for t in node.targets):
            return ast.literal_eval(node.value)
    return {}


def propose(run_id: str, path: str, conf_changes: dict[str, str], evidence: list[str], summary: str) -> Proposal:
    problems = []
    if not ALLOWED_FILES.match(path):
        problems.append(f"{path}: the agent may only change DAG files")
    bad_keys = sorted(set(conf_changes) - ALLOWED_SPARK_KEYS)
    if bad_keys:
        problems.append(f"not on the tuning allow-list: {bad_keys}")
    branch = f"agent/{run_id}"
    if problems:
        return Proposal(False, branch, "", problems, "")

    work = Path(tempfile.mkdtemp(prefix="data-agent-"))
    try:
        shutil.copytree(REPO, work, dirs_exist_ok=True)
        _git(work, "init", "-q", "-b", "main")
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", "baseline")
        _git(work, "checkout", "-qb", branch)

        target = work / path
        source = target.read_text()
        before = _spark_conf(source)
        for key, value in conf_changes.items():
            line = f'    "{key}": "{value}",'
            pattern = re.compile(rf'^    "{re.escape(key)}": "[^"]*",$', re.MULTILINE)
            if key in before:
                source = pattern.sub(line, source)
            else:   # add a new key just before the dict closes
                source = re.sub(r"(SPARK_CONF = \{\n(?:    .*\n)*?)(\})", rf"\1{line}\n\2", source, count=1)
        target.write_text(source)

        try:
            compile(source, path, "exec")
        except SyntaxError as e:
            problems.append(f"patched file does not compile: {e}")
        after = _spark_conf(source)
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        if changed - ALLOWED_SPARK_KEYS:
            problems.append(f"unexpected changes: {sorted(changed - ALLOWED_SPARK_KEYS)}")
        diff = _git(work, "diff")
        n = sum(1 for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---"))
        if n > MAX_CHANGED_LINES:
            problems.append(f"diff too large ({n} lines > {MAX_CHANGED_LINES})")
        if not problems:
            _git(work, "commit", "-qam", f"[agent] {summary}")
        pr = (f"## {summary}\n\nProposed automatically by the data platform agent for failed run `{run_id}`. "
              "Needs review by the job owner.\n\n### Evidence\n" + "\n".join(f"- {e}" for e in evidence) +
              "\n\n### Change\n" + "\n".join(f"- `{k}`: `{before.get(k, '(unset)')}` -> `{v}`"
                                             for k, v in conf_changes.items()) +
              "\n\n### Checks\n- only allow-listed Spark tuning keys changed\n- DAG file compiles\n"
              f"- {n} changed lines\n")
        return Proposal(not problems, branch, diff, problems, pr)
    finally:
        shutil.rmtree(work, ignore_errors=True)
