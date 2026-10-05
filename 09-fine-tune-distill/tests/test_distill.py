import json

from distill.data import generate, prepare, text_hash, write_sft_jsonl
from distill.student import cascade, train_student


def test_hygiene_dedupes_redacts_and_never_leaks_test_into_train():
    train, test, rep = prepare(generate(1500))
    assert rep["duplicates_removed"] > 0 and rep["redacted"] > 0
    assert not {text_hash(e.text) for e in train} & {text_hash(e.text) for e in test}
    assert max(e.created for e in train) < min(e.created for e in test)          # time split
    assert all("@acme.example" not in e.text and "AKIA" not in e.text for e in train + test)


def test_sft_jsonl_is_chat_format_with_provenance(tmp_path):
    train, _, _ = prepare(generate(200))
    write_sft_jsonl(train, tmp_path / "t.jsonl")
    row = json.loads((tmp_path / "t.jsonl").read_text().splitlines()[0])
    assert [m["role"] for m in row["messages"]] == ["system", "user", "assistant"]
    assert row["meta"]["teacher_model"] and row["meta"]["prompt_version"]


def test_drift_hurts_the_student_and_the_cascade_recovers_most_of_it():
    train, test, _ = prepare(generate())
    points = {round(p.threshold, 2): p for p in cascade(train_student(train), test)}
    student_only, teacher_only, mid = points[0.0], points[1.01], points[0.8]
    assert student_only.accuracy < teacher_only.accuracy - 0.1          # new vocabulary in the test window
    assert mid.accuracy >= teacher_only.accuracy - 0.01                  # cascade keeps quality...
    assert mid.usd_per_1k < 0.6 * teacher_only.usd_per_1k                # ...at a fraction of the cost
