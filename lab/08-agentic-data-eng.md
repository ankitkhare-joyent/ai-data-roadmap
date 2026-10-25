# Lab 08 · Agentic data engineering: lineage, contracts, and an agent that knows when not to patch

One sitting, about 35 minutes, no Docker. Background: [lesson 8](../08-agentic-data-eng/README.md). Code:
[platform.py](../08-agentic-data-eng/src/data_agent/platform.py), [triage.py](../08-agentic-data-eng/src/data_agent/triage.py),
[patcher.py](../08-agentic-data-eng/src/data_agent/patcher.py), [evals.py](../08-agentic-data-eng/src/data_agent/evals.py),
fixtures in [data/](../08-agentic-data-eng/data).

After this lab you can explain:

- what an OpenLineage event carries, and how a schema change shows up in lineage before anything fails
- how blast radius is computed, and what a gap in lineage coverage does to it
- what the patcher lets an agent change, and what it refuses however well argued
- why rule order matters in triage, and how an eval catches a misdiagnosis

## Before you start

```bash
cd ~/workstuff/argocd-terraform-opentofu/ai-data-roadmap/08-agentic-data-eng
mise trust && mise install
mise run reset                  # installs deps and checks the fixtures are unmodified
source ../lab/env.sh            # sets PYTHONPATH=src
```

## 1. Lineage events, raw

```bash
jq -r .eventType data/lineage/events.jsonl | sort | uniq -c
jq -c 'select(.eventType=="FAIL") | {run: .run.runId, job: .job.name, error: .run.facets.errorMessage.message[:70]}' data/lineage/events.jsonl
jq -c 'select(.job.name=="ec2_inventory.ingest_snapshots") | {t: .eventTime, cols: [.outputs[0].facets.schema.fields[].name]}' data/lineage/events.jsonl
```

**Predict (P1):** the third command lists the columns the ingest job *wrote*, per run. What changed, and when,
relative to the `run-drift` failure (2026-10-04 02:41)?

## 2. The graph and the contract

```bash
uv run python -c "
from data_agent.platform import Lineage, validate_sample
l = Lineage()
print('downstream of ec2.instances_raw:', l.downstream('ec2.instances_raw'))
print('producers of ec2.instances:', l.producer_of['ec2.instances'])
print('contract check:', validate_sample('ec2.instances_raw'))"
```

**Predict (P2):** which jobs and datasets are downstream of the raw table? What does the contract check find in
the latest sample besides the rename?

## 3. Triage, and the action it picks

```bash
uv run python -m data_agent.triage | grep -E "^== | -> "
```

**Predict (P3):** before you run it, write down the action you would take for each of the four failures: patch,
issue, rerun or escalate.

## 4. What the patcher allows

```bash
uv run python -c "
from data_agent import patcher
cases = [
 ('tuning', 'dags/ec2_inventory.py', {'spark.executor.memory': '8g', 'spark.sql.adaptive.skewJoin.enabled': 'true'}),
 ('iam', 'dags/ec2_inventory.py', {'execution_role_arn': 'arn:aws:iam::392910111111:role/admin'}),
 ('terraform', 'infra/iam.tf', {'spark.executor.memory': '8g'}),
 ('every key', 'dags/ec2_inventory.py', {'spark.executor.memory': '16g', 'spark.executor.memoryOverhead': '4g',
   'spark.executor.instances': '80', 'spark.sql.shuffle.partitions': '8000', 'spark.sql.adaptive.enabled': 'false',
   'spark.sql.adaptive.skewJoin.enabled': 'true', 'spark.sql.adaptive.advisoryPartitionSizeInBytes': '256m'}),
]
for name, path, ch in cases:
    p = patcher.propose('lab-' + name.replace(' ', '-'), path, ch, ['lab'], 'lab ' + name)
    print(f'{name:<10} ok={p.ok} branch={p.branch} problems={p.problems}')
print(patcher.propose('lab-pr', 'dags/ec2_inventory.py', {'spark.executor.memory': '8g'}, ['OOMKilled at 4.5 GiB'], 'Raise executor memory').pr_body)"
git status --short repo
```

**Predict (P4):** which of the four proposals pass? The diff cap is 12 changed lines: does "every key" (all seven
allow-listed keys at once) hit it? And after all this, has `repo/` changed?

## Break it, then fix it

**Break 1: a job stops emitting lineage.** Superset's integration gets switched off, so the dashboard's event is
missing:

```bash
grep -v '"superset://prod"' data/lineage/events.jsonl > /tmp/events.jsonl && cp /tmp/events.jsonl data/lineage/events.jsonl
uv run python -m data_agent.evals
```

**Predict (P5):** which incidents lose points, and on which check? Does triage itself notice anything is wrong?

**Fix:** `git checkout -- data/lineage/events.jsonl`.

**Break 2: a noisy log.** Spark logs are full of warnings from earlier, recovered problems. Add one to the
schema-drift run's log:

```bash
echo '[2026-10-04, 02:40:59 UTC] {emr_containers.py:180} INFO - driver: WARN executor 3 restarted earlier: Container killed: OOMKilled (recovered)' \
  >> data/logs/run-drift.log
uv run python -m data_agent.triage | sed -n '/run-drift/,$p' | head -4
uv run python -m data_agent.evals | grep run-drift
```

**Predict (P6):** the real cause is still the renamed column. What does triage decide now, and what would happen if
nobody reviewed the PR?

**Fix:** `git checkout -- data/logs/run-drift.log && uv run python -m data_agent.evals | tail -1`. The lasting fix is
in the classifier: rank evidence by how specific it is (an `UNRESOLVED_COLUMN` naming the failing stage beats a
recovered warning), or let Claude weigh it (`mise run triage-claude`) and keep these incidents as eval cases.

## Check your predictions

**P1.** `instanceType` became `instance_type` in the **2026-10-04 02:12** ingest; the 10-02 and 10-03 runs still
wrote `instanceType`. The schema facet showed the rename 29 minutes before `flatten_instances` failed on it. An
agent watching schema facets against the contract could have flagged the producer before any consumer broke.

**P2.** Jobs: `flatten_instances`, `finance.daily_ec2_cost`, `ml.rightsizing_features` and
`superset://prod/fleet_cost_dashboard`; datasets: `ec2.instances`, `finance.ec2_daily_cost`,
`ml.rightsizing_features`. The only producer of `ec2.instances` is `flatten_instances`. The contract check (v2.3.0,
owner `cloud-inventory-oncall@acme.example`) finds `missing_required: ['instanceType']`,
`unexpected_columns: ['instance_type']`, and an invalid state value: `{'state': ['stoped']}`, a typo the
producer's own validation should have caught.

**P3.**

```text
run-oom    resources -> PATCH     Spark executors were OOM-killed on a skewed partition
run-late   upstream_late -> RERUN Upstream ['ec2_inventory.flatten_instances'] had not produced today's data
run-perm   permissions -> ESCALATE Role lacks glue:GetTable on arn:aws:glue:...:table/ml/rightsizing_features
run-drift  schema_drift -> ISSUE  Upstream renamed `instanceType` to `instance_type` in ec2.instances_raw
```

Only one of four is a code change, and even that goes to review. If you'd have patched the consumer for the drift,
that's the instinct this lesson is about: the producer broke a contract, so the fix (and the conversation) belongs
to them.

**P4.** `tuning` passes on branch `agent/lab-tuning`. `iam` fails: `not on the tuning allow-list:
['execution_role_arn']`. `terraform` fails: `infra/iam.tf: the agent may only change DAG files`. `every key`
**passes**: five changed keys (two lines each) plus two new ones is exactly 12 lines, the cap. With the allow-list in
place the size cap can never trigger on its own; it's a backstop in case the allow-list grows. The PR body carries
the evidence (`OOMKilled at 4.5 GiB`), the before/after (`4g -> 8g`) and the checks. `git status repo` prints
nothing: every proposal ran in a throwaway clone.

**P5.** `run-oom`, `run-late` and `run-drift` drop to 0.85, all on **blast radius**, and the mean falls to
0.89; only `run-perm` (whose job has nothing downstream) keeps full marks. Triage notices nothing: a missing edge
looks exactly like "no dependency". Lineage is only as complete as the integrations that emit it, which is why
coverage (every job and every BI tool emitting events) is a platform metric, not a nice-to-have.

**P6.** `resources -> PATCH: Spark executors were OOM-killed on a skewed partition`, with a proposal on
`agent/run-drift` that passes every check, and the eval scores `run-drift 0.15  failed: action, category, owner`.
The rules test for OOM before schema errors, and one recovered warning in the log was enough. Merged, the PR
doubles executor memory, the job still fails on the missing column, and the producer is never told. That's the
case for human review on every agent PR, and for keeping incidents like this one in the eval suite.

## What to remember

- OpenLineage schema facets show drift at the producer, before consumers fail.
- Blast radius is a graph walk, and a missing integration silently shrinks it.
- Give the agent a narrow, enforced write path: allow-listed keys in allow-listed files, in a sandbox, as a PR.
- Rule-based triage is order-sensitive; evals over real incidents are how you find out.

Next: [Lab 09 · Fine-tuning and distillation](09-fine-tune-distill.md).
