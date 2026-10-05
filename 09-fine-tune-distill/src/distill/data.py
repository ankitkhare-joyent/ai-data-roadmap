"""Build a distillation dataset from teacher outputs, with the hygiene that decides whether it works.

In production the teacher is Claude labelling real tickets through the Batch API (lesson 5's enrichment
table *is* this dataset: every row has the input, the output, the prompt version and the model). Here the
tickets are synthetic and the teacher is simulated as 97% accurate, so the lesson runs offline in seconds.

Hygiene steps, each of which has sunk real projects when skipped:
  redact       no secrets or personal data in training data: a fine-tuned model can regurgitate it
  dedupe       exact duplicates inflate the training set and leak into the test set
  time split   train on older data, test on the newest: random splits overstate accuracy on drifting data
  decontam     no test example may appear in training, checked by content hash
  versioned    every example records the teacher model and prompt version it came from
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

CATEGORIES = ["outage", "capacity", "cost", "security", "access"]
TEMPLATES = {   # how people wrote tickets for most of the period
    "outage": ["{svc} on {iid} is down, dashboards failing", "{svc} crashed on {iid}, nothing running",
               "{iid} unreachable, health checks red, {svc} users impacted", "Production {svc} returns 503 on {iid}"],
    "capacity": ["Spark executors on {iid} OOM killed again", "Disk on {iid} at 97%, compaction failing",
                 "CPU on {iid} pinned at 90% during the {svc} batch", "{svc} queue backing up, {iid} too small"],
    "cost": ["{iid} idle for 3 weeks, can we stop it?", "Dev box {iid} left running all weekend",
             "{svc} test cluster on {iid} costs more than prod", "Unattached volumes from {iid} still billed"],
    "security": ["Security group on {iid} open to 0.0.0.0/0 on 22, please lock down",
                 "IMDSv1 still enabled on {iid}", "{svc} credentials found in a public bucket, rotate keys",
                 "Unusual login to {iid} from an unknown IP"],
    "access": ["Need SSM access to {iid} to debug {svc}", "Please add me to the IAM role used by {iid}",
               "AccessDenied reading {svc} tables from {iid}", "Grant read on the {svc} Glue database"],
}
# New phrasings that only appear in the last 12 days: a new team, a new alerting tool, a new vocabulary.
# This is drift. A model trained on older tickets has never seen these words; the teacher (an LLM)
# understands them anyway. It's why you time-split, and why the cascade keeps the teacher in the loop.
NEW_TEMPLATES = {
    "outage": ["PagerDuty P1: {svc} SLO burn on {iid}, error budget gone", "{svc} brownout, p99 timeouts on {iid}"],
    "capacity": ["Karpenter cannot schedule {svc} pods, {iid} node pool exhausted",
                 "{svc} shuffle spill to disk on {iid}, jobs crawling"],
    "cost": ["FinOps flagged {iid}: rightsizing candidate, utilisation under 3%",
             "Savings Plan coverage dropped, {svc} on-demand spend spiking on {iid}"],
    "security": ["GuardDuty finding on {iid}: crypto-mining behaviour", "Inspector CVE critical on {svc} image on {iid}"],
    "access": ["Lake Formation grant missing for {svc} on {iid}", "Break-glass role request for {iid} to fix {svc}"],
}
REGIONS = ["eu-west-1", "us-east-1", "ap-south-1"]
SERVICES = ["Trino", "Airflow", "Superset", "Spark", "Kafka", "Flink", "StarRocks"]
NOISE = ["thanks", "urgent", "fyi", "again", "since yesterday", "per alert", "see thread", "cc oncall"]
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")


@dataclass
class Example:
    id: str
    created: int          # day number, for the time split
    text: str
    gold: str             # human truth (only for evaluation; never used for training)
    teacher: str          # what the teacher model said
    teacher_model: str = "claude-sonnet-5-5"
    prompt_version: str = "triage-v1"


def _iid(rng: random.Random) -> str:
    return "i-0" + "".join(rng.choice("0123456789abcdef") for _ in range(16))


def generate(n: int = 4000, seed: int = 1, teacher_accuracy: float = 0.97) -> list[Example]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        cat = rng.choice(CATEGORIES)
        day = i * 90 // n
        pool = TEMPLATES[cat] + (NEW_TEMPLATES[cat] * 2 if day >= 78 else [])   # new phrasing in the last 12 days
        text = rng.choice(pool).format(iid=_iid(rng), svc=rng.choice(SERVICES))
        text += f" ({rng.choice(REGIONS)}, {rng.randint(0, 23):02d}:{rng.randint(0, 59):02d} UTC) "
        text += " ".join(rng.sample(NOISE, rng.randint(0, 2)))
        if rng.random() < 0.03:
            text += f" contact {rng.choice(['priya', 'marco', 'li'])}@acme.example"     # PII to redact
        if rng.random() < 0.01:
            text += " key AKIA" + "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(16))
        if rng.random() < 0.06 and out:
            prev = rng.choice(out)
            text, cat = prev.text, prev.gold                                              # duplicate report
        teacher = cat if rng.random() < teacher_accuracy else rng.choice([c for c in CATEGORIES if c != cat])
        out.append(Example(f"OPS-{i:06d}", created=day, text=text.strip(), gold=cat, teacher=teacher))
    return out


def redact(text: str) -> str:
    return KEY.sub("[KEY]", EMAIL.sub("[EMAIL]", text))


def text_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"i-0[0-9a-f]{16}", "i-X", text.lower()).encode()).hexdigest()  # same ticket, other box


def prepare(examples: list[Example], test_days: int = 15) -> tuple[list[Example], list[Example], dict]:
    """Redact, dedupe, split by time, decontaminate. Returns train, test and a report."""
    seen, clean, dupes = set(), [], 0
    for e in examples:
        e.text = redact(e.text)
        h = text_hash(e.text)       # instance ids normalised: the same ticket about another box is a duplicate
        if h in seen:
            dupes += 1
            continue
        seen.add(h)
        clean.append(e)
    cutoff = max(e.created for e in clean) - test_days
    train = [e for e in clean if e.created <= cutoff]
    test = [e for e in clean if e.created > cutoff]
    train_hashes = {text_hash(e.text) for e in train}
    leaked = [e for e in test if text_hash(e.text) in train_hashes]
    test = [e for e in test if text_hash(e.text) not in train_hashes]
    report = {"input": len(examples), "duplicates_removed": dupes, "train": len(train), "test": len(test),
              "decontaminated": len(leaked), "redacted": sum("[EMAIL]" in e.text or "[KEY]" in e.text for e in clean)}
    return train, test, report


SYSTEM = f"Classify the EC2 ops ticket into one of {CATEGORIES}. Reply with the category only."


def write_sft_jsonl(examples: list[Example], path: Path) -> int:
    """Chat-format SFT data (the layout TRL's SFTTrainer and most fine-tuning services accept)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in examples:
            f.write(json.dumps({"messages": [{"role": "system", "content": SYSTEM},
                                             {"role": "user", "content": e.text},
                                             {"role": "assistant", "content": e.teacher}],
                                "meta": {"id": e.id, "teacher_model": e.teacher_model,
                                         "prompt_version": e.prompt_version}}) + "\n")
    return len(examples)
