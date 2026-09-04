"""Public calendar knowledge, shared by both the desk and the simulator.

This module exists so that the one fact both sides legitimately share is
explicit and auditable, rather than smuggled across the circularity firewall by
an import from the simulator into the decision core.

The desk knows *when salary credits typically land*. It does not know when any
individual customer's balance actually recovers -- that is latent, drawn with
jitter around these days, and lives in ``fixtures/world.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: Days of the month on which salary credits typically land in the Indian market.
SALARY_CREDIT_DAYS = (1, 25)


def next_salary_credit(after: datetime) -> datetime:
    """First salary-credit day strictly after ``after``."""
    cursor = after
    for _ in range(70):
        cursor = cursor + timedelta(days=1)
        if cursor.day in SALARY_CREDIT_DAYS:
            return cursor.replace(hour=11, minute=0, second=0, microsecond=0)
    return after + timedelta(days=30)
