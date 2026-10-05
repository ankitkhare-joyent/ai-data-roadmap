"""The student, and the cascade that decides when the student is good enough to answer alone.

Start with the cheapest student that could work. For classification that's often not an LLM at all: a
TF-IDF + logistic regression model trains in a second on CPU, serves in microseconds, and is a baseline
any fine-tuned LLM must beat to justify itself. jobs/train_lora.py is the LLM student for when it doesn't.

The cascade: the student answers when it is confident; otherwise the request goes to the teacher (Claude,
through the gateway). The threshold trades accuracy for cost, and you pick it on held-out data.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

from distill.data import Example

TEACHER_USD_PER_CALL = 2e-6 * 250 + 10e-6 * 5      # Sonnet 5.5: ~250 input, ~5 output tokens per ticket
STUDENT_USD_PER_CALL = 2e-7                         # a CPU pod's share; effectively free


def train_student(train: list[Example]):
    model = make_pipeline(
        TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True, token_pattern=r"(?u)\b[a-z][a-z0-9.]+\b"),
        # Calibrated probabilities: the cascade threshold only means something if 0.9 means ~90% right.
        CalibratedClassifierCV(LogisticRegression(max_iter=2000, C=4.0), cv=3),
    )
    model.fit([e.text for e in train], [e.teacher for e in train])     # learns from the TEACHER, not gold
    return model


@dataclass
class CascadePoint:
    threshold: float
    coverage: float        # share answered by the student
    accuracy: float        # against human gold labels
    usd_per_1k: float


def cascade(model, test: list[Example], thresholds=(0.0, 0.5, 0.7, 0.8, 0.9, 0.95, 0.99, 1.01)) -> list[CascadePoint]:
    proba = model.predict_proba([e.text for e in test])
    pred = model.classes_[proba.argmax(1)]
    conf = proba.max(1)
    gold = np.array([e.gold for e in test])
    teacher = np.array([e.teacher for e in test])
    out = []
    for t in thresholds:
        use_student = conf >= t
        answer = np.where(use_student, pred, teacher)
        cost = use_student.sum() * STUDENT_USD_PER_CALL + (~use_student).sum() * TEACHER_USD_PER_CALL
        out.append(CascadePoint(t, float(use_student.mean()), float((answer == gold).mean()),
                                float(cost / len(test) * 1000)))
    return out
