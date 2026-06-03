"""Rule-based classifiers — keyword/heuristic backends (the default, no deps).

These hold the keyword and mapping logic that previously lived inline in
``app/worker/tasks.py``. They are always available (no ML dependencies) and are
the default backend, so the standard stack runs without torch/transformers.
"""

import re

from app.enums import Category, Priority

# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------

_CRITICAL_KEYWORDS = (
    "outage",
    "down",
    "critical",
    "emergency",
    "urgent",
    "production",
)
_HIGH_KEYWORDS = (
    "asap",
    "important",
    "major",
    "severe",
    "degraded",
    "performance",
)

# Precompiled word-boundary patterns prevent false positives from substrings:
# "down" won't match "download"/"markdown"; "production" won't match inside
# "nonproduction". Patterns are built once at import time.
_CRITICAL_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _CRITICAL_KEYWORDS) + r")\b"
)
_HIGH_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _HIGH_KEYWORDS) + r")\b"
)


class RulesPriorityClassifier:
    """Keyword word-boundary matching → priority floor."""

    def is_available(self) -> bool:
        return True

    async def classify_priority(self, subject: str, description: str) -> Priority:
        """Return keyword-detected priority, or LOW when no signal is found.

        LOW is the detection floor: assign_priority only upgrades when computed
        rank exceeds the customer's submitted priority, so no keywords means no
        change.
        """
        text = (subject + " " + description).lower()
        if _CRITICAL_RE.search(text):
            return Priority.CRITICAL
        if _HIGH_RE.search(text):
            return Priority.HIGH
        return Priority.LOW


# ---------------------------------------------------------------------------
# Spam
# ---------------------------------------------------------------------------

_SPAM_PHRASES = (
    "click here",
    "free offer",
    "you have won",
    "congratulations",
    "act now",
    "limited time",
    "buy now",
    "make money",
)
_MAX_URLS = 2


class RulesSpamClassifier:
    """Known-phrase and URL-count heuristics → spam flag."""

    def is_available(self) -> bool:
        return True

    async def classify_spam(self, subject: str, description: str) -> bool:
        text = (subject + " " + description).lower()
        url_count = text.count("http://") + text.count("https://")
        if url_count > _MAX_URLS:
            return True
        return any(phrase in text for phrase in _SPAM_PHRASES)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

_DEPARTMENT: dict[Category, str] = {
    Category.BILLING: "billing",
    Category.TECHNICAL: "technical-support",
    Category.FEATURE_REQUEST: "product",
    Category.OTHER: "general",
}

# Canonical destination departments — ML backends must map into this set so the
# ROUTED event value space never changes.
DEPARTMENTS = tuple(_DEPARTMENT.values())


class RulesRoutingClassifier:
    """Deterministic category → department map (ignores ticket text)."""

    def is_available(self) -> bool:
        return True

    async def classify_department(
        self, category: Category, subject: str, description: str
    ) -> str:
        return _DEPARTMENT[category]
