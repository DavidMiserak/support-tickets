"""Unit tests for the rule-based classifiers (the default, no-deps backend).

These lock the keyword/mapping logic in place independent of the task layer.
"""

import pytest

from app.enums import Category, Priority
from app.worker.backends.rules_classifier import (
    RulesPriorityClassifier,
    RulesRoutingClassifier,
    RulesSpamClassifier,
)

pytestmark = pytest.mark.no_auto_schema


# --- Priority -------------------------------------------------------------


async def test_priority_critical_keyword() -> None:
    clf = RulesPriorityClassifier()
    assert await clf.classify_priority("Production outage", "everything is down") == (
        Priority.CRITICAL
    )


async def test_priority_high_keyword() -> None:
    clf = RulesPriorityClassifier()
    assert (
        await clf.classify_priority("Important request", "this is major")
        == Priority.HIGH
    )


async def test_priority_floor_low_without_keywords() -> None:
    clf = RulesPriorityClassifier()
    assert (
        await clf.classify_priority("Question about my plan", "how do I upgrade?")
        == Priority.LOW
    )


async def test_priority_ignores_substring_false_positive() -> None:
    """Word-boundary regex: 'download' must not match the 'down' keyword."""
    clf = RulesPriorityClassifier()
    assert (
        await clf.classify_priority("Download link", "the download is slow")
        == Priority.LOW
    )


# --- Spam -----------------------------------------------------------------


async def test_spam_known_phrase() -> None:
    clf = RulesSpamClassifier()
    assert await clf.classify_spam("Congratulations", "you have won a free offer")


async def test_spam_excessive_urls() -> None:
    clf = RulesSpamClassifier()
    desc = "see http://a.com http://b.com https://c.com"
    assert await clf.classify_spam("links", desc)


async def test_spam_clean_ticket() -> None:
    clf = RulesSpamClassifier()
    assert not await clf.classify_spam("Login broken", "I cannot sign in to my account")


# --- Routing --------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (Category.BILLING, "billing"),
        (Category.TECHNICAL, "technical-support"),
        (Category.FEATURE_REQUEST, "product"),
        (Category.OTHER, "general"),
    ],
)
async def test_routing_per_category(category: Category, expected: str) -> None:
    clf = RulesRoutingClassifier()
    assert await clf.classify_department(category, "subj", "desc") == expected
