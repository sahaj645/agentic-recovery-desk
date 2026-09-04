"""Deterministic failure classification from raw gateway text.

This is the ablation arm. It is a keyword matcher over the formats we have
seen, and it is honest about its own limits: unmatched text becomes UNKNOWN
rather than a confident guess. Every point of accuracy the model arm buys over
this file is the model's contribution, stated as a number.
"""

from __future__ import annotations

import re

from ..models import FailureClass

# Ordered because specificity matters: "insufficient funds after retry timeout"
# is a balance failure, not a timeout, and the balance rule must win.
_PATTERNS: tuple[tuple[FailureClass, re.Pattern[str]], ...] = (
    (
        FailureClass.ACCOUNT_BLOCKED,
        re.compile(r"blocked|frozen|freeze|dormant|restricted|lien|closed\s+account", re.I),
    ),
    (
        FailureClass.INSUFFICIENT_BALANCE,
        re.compile(r"insufficient|low\s+balance|nsf|not\s+enough\s+funds|exceeds\s+balance", re.I),
    ),
    (
        FailureClass.WRONG_PIN,
        re.compile(
            r"incorrect\s+(m?pin|password)|invalid\s+(m?pin|otp)|attempts\s+exceed"
            r"|wrong\s+pin|auth(entication)?\s+fail",
            re.I,
        ),
    ),
    (
        FailureClass.NETWORK,
        re.compile(r"network|connectivity|unreachable|socket|dns|link\s+down", re.I),
    ),
    (
        FailureClass.BANK_TIMEOUT,
        re.compile(
            r"timeout|timed\s*out|no\s+response|gateway\s+error|issuer\s+down"
            r"|upstream|5\d{2}\b|deadline",
            re.I,
        ),
    ),
)


def classify_text(raw: str) -> tuple[FailureClass, float, str]:
    """Return (class, confidence, evidence).

    Confidence is the matcher's own honesty, not a probability: a single clean
    keyword hit is 0.80, an unmatched string is 0.25 on UNKNOWN.
    """
    for failure_class, pattern in _PATTERNS:
        match = pattern.search(raw)
        if match:
            return failure_class, 0.80, "matched " + repr(match.group(0))
    return FailureClass.UNKNOWN, 0.25, "no known pattern matched"
