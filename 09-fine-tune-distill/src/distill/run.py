"""The whole distillation loop on CPU in seconds: dataset, student, cascade, decision.

    python -m distill.run
"""
from __future__ import annotations

from pathlib import Path

from distill.data import generate, prepare, write_sft_jsonl
from distill.student import cascade, train_student

OUT = Path(__file__).resolve().parents[2] / ".data"


def main() -> None:
    train, test, rep = prepare(generate())
    print("== 1. dataset hygiene")
    print("   " + ", ".join(f"{k}={v}" for k, v in rep.items()))
    n = write_sft_jsonl(train, OUT / "train.jsonl")
    write_sft_jsonl(test, OUT / "test.jsonl")
    print(f"   wrote {n} chat-format training examples to .data/train.jsonl (for jobs/train_lora.py)")

    teacher_acc = sum(e.teacher == e.gold for e in test) / len(test)
    model = train_student(train)
    student_acc = sum(p == e.gold for p, e in zip(model.predict([e.text for e in test]), test, strict=True)) / len(test)
    print("\n== 2. accuracy on the newest 15 days (human labels)")
    print(f"   teacher (Claude, simulated)  {teacher_acc:.3f}")
    print(f"   student (TF-IDF + LR, CPU)   {student_acc:.3f}   trained on teacher labels only")

    print("\n== 3. cascade: the student answers when confident, otherwise the teacher does")
    print(f"   {'threshold':>9} {'student share':>14} {'accuracy':>9} {'$ per 1k tickets':>17}")
    points = cascade(model, test)
    for p in points:
        label = "teacher only" if p.threshold > 1 else ("student only" if p.threshold == 0 else f"{p.threshold:.2f}")
        print(f"   {label:>12} {p.coverage:>11.0%} {p.accuracy:>9.3f} {p.usd_per_1k:>17.3f}")
    target = teacher_acc - 0.005
    best = min((p for p in points if p.accuracy >= target), key=lambda p: p.usd_per_1k)
    teacher_cost = points[-1].usd_per_1k
    print(f"\n== 4. decision: threshold {best.threshold:.2f} keeps accuracy within half a point of the teacher "
          f"({best.accuracy:.3f}) while the student handles {best.coverage:.0%} of tickets: "
          f"{1 - best.usd_per_1k / teacher_cost:.0%} cheaper than sending everything to Claude.")


if __name__ == "__main__":
    main()
