"""Stage 1. Pull at-risk items and normalise them to a common shape.

Two sources feed the desk today -- the payments stream and the subscription
schedule -- and both arrive as ``AtRiskItem``. The stage is deliberately thin:
its only job is to make everything downstream source-agnostic, so adding a
third leak class later means adding an adapter, not touching the decision core.
"""

from __future__ import annotations

from typing import Sequence

from ..models import AtRiskItem, ItemType

#: What the desk currently knows how to work on. An item type outside this set
#: is dropped at ingest with a count, never silently carried into the decision
#: core where it would be priced with a prior that does not apply to it.
SUPPORTED_TYPES = frozenset({ItemType.PAYMENT_FAILURE, ItemType.SUBSCRIPTION_DUE})


def ingest(items: Sequence[AtRiskItem]) -> list[AtRiskItem]:
    """Normalise and admit. Ordering is by occurrence, which is how a desk sees them."""
    admitted = [item for item in items if item.type in SUPPORTED_TYPES]
    return sorted(admitted, key=lambda i: (i.occurred_at, i.id))


def dropped_count(items: Sequence[AtRiskItem]) -> int:
    return sum(1 for item in items if item.type not in SUPPORTED_TYPES)
