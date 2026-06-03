"""Print zero-shot classifier decisions for a few sample tickets.

Mirrors scripts/demo_ml_summary.py for the priority/spam/routing classifiers:
loads the shared zero-shot pipeline once and prints, per sample, the winning
label + confidence and the mapped decision for each task.

Run via: make test-ml   or   python -m scripts.demo_ml_classify
Requires: pip install -r requirements-ml.txt
"""

from __future__ import annotations

import asyncio
import sys
from typing import TypedDict

from app.enums import Category
from app.worker.backends.zeroshot_classifier import (
    _DEPARTMENT_LABELS,
    _HAM_LABEL,
    _PRIORITY_LABELS,
    _ROUTING_CONFIDENCE_FLOOR,
    _SPAM_LABEL,
    _SPAM_THRESHOLD,
    ZeroShotPipeline,
    ZeroShotRoutingClassifier,
)
from app.worker.ml_sample_text import ML_SAMPLE_TICKET_DESCRIPTION


class _Sample(TypedDict):
    label: str
    subject: str
    description: str
    category: Category


_SAMPLES: list[_Sample] = [
    {
        "label": "Technical outage (shared sample)",
        "subject": "Cannot access staging after maintenance",
        "description": ML_SAMPLE_TICKET_DESCRIPTION,
        "category": Category.TECHNICAL,
    },
    {
        "label": "Promotional spam",
        "subject": "Congratulations!!! You have WON",
        "description": (
            "Click here to claim your free offer now. Limited time only — "
            "buy now and make money fast! http://spam.example http://promo.example"
        ),
        "category": Category.OTHER,
    },
    {
        "label": "Billing refund",
        "subject": "Double charged for my subscription",
        "description": (
            "I was billed twice this month for my subscription and would like a "
            "refund for the duplicate invoice charge on my credit card."
        ),
        "category": Category.BILLING,
    },
]


def _top(scores: dict[str, float]) -> tuple[str, float]:
    label = max(scores, key=lambda k: scores[k])
    return label, scores[label]


async def _run() -> int:
    pipeline = ZeroShotPipeline()
    if not pipeline.is_available():
        print("ML dependencies not installed. Run: make install-ml", file=sys.stderr)
        return 1

    print("Loading zero-shot model (first run may download ~700 MB)...")
    pipeline.load_model()
    print("Classifying sample tickets...\n")

    routing_clf = ZeroShotRoutingClassifier(pipeline)

    for sample in _SAMPLES:
        text = sample["subject"] + ". " + sample["description"]

        prio_scores = await pipeline.classify(text, list(_PRIORITY_LABELS))
        prio_label, prio_conf = _top(prio_scores)
        priority = _PRIORITY_LABELS[prio_label]

        spam_scores = await pipeline.classify(text, [_SPAM_LABEL, _HAM_LABEL])
        spam_conf = spam_scores[_SPAM_LABEL]
        flagged = spam_conf > _SPAM_THRESHOLD

        dept_scores = await pipeline.classify(text, list(_DEPARTMENT_LABELS))
        dept_label, dept_conf = _top(dept_scores)
        department = await routing_clf.classify_department(
            sample["category"], sample["subject"], sample["description"]
        )
        dept_note = (
            f", below {_ROUTING_CONFIDENCE_FLOOR:.2f} floor → category rule"
            if dept_conf < _ROUTING_CONFIDENCE_FLOOR
            else ""
        )

        desc = sample["description"]
        snippet = desc if len(desc) <= 150 else desc[:150] + "..."

        print(f"=== {sample['label']} ===")
        print(f"Subject    : {sample['subject']}")
        print(f"Description: {snippet}")
        print(f"(stated category: {sample['category'].value})\n")
        print(
            f"  Priority   : {priority.value:<9} "
            f"(label {prio_label!r}, score {prio_conf:.2f})"
        )
        print(
            f"  Spam       : {'YES' if flagged else 'NO':<9} "
            f"(spam score {spam_conf:.2f}, threshold {_SPAM_THRESHOLD:.2f})"
        )
        print(
            f"  Department : {department:<9} "
            f"(zero-shot {dept_label!r} {dept_conf:.2f}{dept_note})"
        )
        print()

    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
