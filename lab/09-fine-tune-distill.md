# Lab 09 · Distillation: splits that tell the truth, a student that drifts, a cascade that saves you

One sitting, about 30 minutes, no Docker, no GPU. Background: [lesson 9](../09-fine-tune-distill/README.md). Code:
[data.py](../09-fine-tune-distill/src/distill/data.py), [student.py](../09-fine-tune-distill/src/distill/student.py),
[run.py](../09-fine-tune-distill/src/distill/run.py), [jobs/train_lora.py](../09-fine-tune-distill/jobs/train_lora.py).

After this lab you can explain:

- what a distillation example looks like on disk, and what hygiene happened before it got there
- why a random split would have told you the student was perfect, and what the time split shows instead
- how a confidence cascade keeps quality under drift, and why thresholds don't carry across models
- what dedupe and decontamination each protect against

## Before you start

```bash
cd ~/workstuff/argocd-terraform-opentofu/ai-data-roadmap/09-fine-tune-distill
mise trust && mise install
mise run reset                  # deletes .data/
source ../lab/env.sh            # sets PYTHONPATH=src
```

## 1. The dataset

```bash
uv run python -m distill.run | head -3
head -1 .data/train.jsonl | jq -c '{roles: [.messages[].role], user: .messages[1].content[:70], label: .messages[2].content, meta}'
grep -m1 -o '"content": "[^"]*\[EMAIL\][^"]*"' .data/train.jsonl
```

**Predict (P1):** 4,000 tickets went in. How many duplicates are removed, and how is a "duplicate" defined when every
ticket names a different instance id? What does the label on a training example come from: a human, or the teacher?

## 2. Time split vs random split

```bash
uv run python -c "
import random
from distill.data import generate, prepare
from distill.student import train_student
train, test, _ = prepare(generate())
acc = lambda m, xs: sum(p == e.gold for p, e in zip(m.predict([e.text for e in xs]), xs)) / len(xs)
m = train_student(train)
newish = lambda e: any(w in e.text for w in ('PagerDuty','brownout','Karpenter','spill','FinOps','Savings Plan','GuardDuty','Inspector','Lake Formation','Break-glass'))
old = [e for e in test if not newish(e)]; new = [e for e in test if newish(e)]
print('time split:', round(acc(m, test), 3), '| old phrasing', round(acc(m, old), 3), len(old), '| new phrasing', round(acc(m, new), 3), len(new))
pool = train + test; random.Random(0).shuffle(pool); k = int(len(pool) * 0.83)
print('random split:', round(acc(train_student(pool[:k]), pool[k:]), 3))"
```

**Predict (P2):** the student trains on the oldest ~75 days and is tested on the newest 15. In the last 12 days
people started writing tickets with new words. What accuracy does it get on old-style and new-style tickets? And
with a random split over the same data?

## 3. The cascade

```bash
uv run python -m distill.run | sed -n '/== 3/,$p'
```

**Predict (P3):** the teacher alone scores 0.978. Can the cascade beat it? At what student share does it stop
paying off?

Now give the student five days of drifted tickets (labelled by the teacher, as you would in production) and test on
the rest:

```bash
uv run python -c "
from distill.data import generate, prepare
from distill.student import train_student
train, test, _ = prepare(generate())
extra = [e for e in test if e.created <= 80]; rest = [e for e in test if e.created > 80]
m = train_student(train + extra)
print('retrained:', round(sum(p == e.gold for p, e in zip(m.predict([e.text for e in rest]), rest)) / len(rest), 3), 'on the last', len(rest))"
```

**Predict (P4):** how much of the gap closes?

## Break it, then fix it

**Break 1: drop calibration.**

```bash
sed -i '' 's/        CalibratedClassifierCV(LogisticRegression(max_iter=2000, C=4.0), cv=3),/        LogisticRegression(max_iter=2000, C=4.0),   # LAB BREAK/' src/distill/student.py
uv run python -m distill.run | sed -n '/threshold/,/teacher only/p'
```

**Predict (P5):** does the cascade stop working? What happens to the meaning of "threshold 0.5"?

**Fix:** `git checkout -- src/distill/student.py`.

**Break 2: skip dedupe.**

```bash
sed -i '' 's/        if h in seen:/        if False:   # LAB BREAK/' src/distill/data.py
uv run python -m distill.run | sed -n 2p
git checkout -- src/distill/data.py
```

**Predict (P6):** with duplicates kept, do near-identical tickets leak from train into test?

Measure what leakage would have done if decontamination were also missing:

```bash
uv run python -c "
from distill.data import generate, redact, text_hash
from distill.student import train_student
ex = generate()
for e in ex: e.text = redact(e.text)
cut = max(e.created for e in ex) - 15
train = [e for e in ex if e.created <= cut]; test = [e for e in ex if e.created > cut]
seen = {text_hash(e.text) for e in train}; leaked = [e for e in test if text_hash(e.text) in seen]; clean = [e for e in test if text_hash(e.text) not in seen]
m = train_student(train); acc = lambda xs: sum(p == e.gold for p, e in zip(m.predict([e.text for e in xs]), xs)) / len(xs)
print('leaked', len(leaked), '| accuracy on leaked', round(acc(leaked), 3), '| test with leaks', round(acc(test), 3), '| clean test', round(acc(clean), 3))"
```

## Check your predictions

**P1.** `duplicates_removed=222`, leaving `train=3143` and `test=635`; `redacted=162` examples had an email or an
AWS key replaced. Duplicates are compared after normalising instance ids (`i-0…` → `i-X`), so "Disk at 97% on box A"
and the same report for box B count as one. The label is the **teacher's** answer (`meta.teacher_model:
claude-sonnet-5-5`, `prompt_version: triage-v1`); human gold labels are used only to evaluate. The `[EMAIL]` and
`[KEY]` placeholders show redaction happened before anything was written.

**P2.** Time split: **0.811** overall, **1.000** on the 406 old-style tickets and **0.476** on the 229 new-style ones.
Random split: **1.000**. The random split mixes the new vocabulary into training, so it reports a perfect model
that would fall to about 81% the week it ships. Time-split anything that drifts.

**P3.** Yes: at threshold 0.80 the student answers 65% of tickets and the cascade scores **0.989**, above the teacher
alone, at $0.193 per 1,000 tickets against $0.550. Confident student answers are very accurate (they even smooth
over the teacher's occasional errors) and the uncertain ones go to the teacher. Below 0.7 the student starts
answering tickets it doesn't understand (0.918 at 0.5); above 0.9 you pay for the teacher on tickets the student
had right.

**P4.** From 0.811 to **0.964** on the last 384 tickets. A few days of teacher-labelled recent data recovers most of
the drift. Watching the share of traffic below the threshold tells you when to do it.

**P5.** It still works (64% student share at 0.992), but every threshold now means something different: at 0.5 the
student takes 72% instead of 81%, and the 0.99 threshold lets 3% through instead of none. Thresholds are properties
of a specific model's probabilities. After every retrain, re-sweep the threshold on held-out data instead of
reusing the old number.

**P6.** `duplicates_removed=0`, and decontamination catches **29** test examples that duplicate a training example
(`decontaminated=29`). Two separate guards: dedupe keeps the training set honest, decontamination keeps the *test*
set honest. With both off, the 29 leaked tickets score 1.000 and the overall test accuracy reads 0.818 instead of
0.810. Small here because only about 4% of tickets are repeats; in alert-storm data, where a third of tickets can
be repeats, the same mistake makes a weak model look excellent.

## What to remember

- Train on the teacher's labels, evaluate on human labels, and record the teacher and prompt version per row.
- Split by time. Random splits hide drift.
- A calibrated student plus a confidence threshold keeps teacher-level quality at a fraction of the cost.
- Dedupe protects training, decontamination protects evaluation; you need both.

Back to the [lab index](README.md).
