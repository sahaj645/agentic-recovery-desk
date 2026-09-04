"""The running state that makes the caps enforceable and the batch replayable.

The ledger is append-only. It is consulted before every dispatch rather than
reconciled after one, because a spend ceiling checked after the fact is not a
ceiling.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import Policy
from ..models import CONTACT_ACTIONS, RETRY_ACTIONS, ActionType


def idempotency_key(run_id: str, item_id: str, action: ActionType, attempt: int) -> str:
    """Stable across replays, unique within one.

    Deriving the key from the batch identity plus the exact action means a
    replayed batch produces the same keys and therefore cannot double-charge or
    double-message. The attempt index is part of the key because a second
    deliberate retry is a different action, not a duplicate of the first.
    """
    raw = "%s|%s|%s|%d" % (run_id, item_id, action.value, attempt)
    return "idm_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass
class Ledger:
    """Mutable running totals for one batch. One ledger per arm, never shared."""

    policy: Policy
    spend: float = 0.0
    retries_by_item: dict[str, int] = field(default_factory=dict)
    contacts_by_customer: dict[str, list[datetime]] = field(default_factory=dict)
    seen_keys: set[str] = field(default_factory=set)
    violations: int = 0
    halted: bool = False

    # -- reads -------------------------------------------------------------

    @property
    def remaining_budget(self) -> float:
        return self.policy.budget - self.spend

    def retries_for(self, item_id: str) -> int:
        return self.retries_by_item.get(item_id, 0)

    def contacts_for(self, customer_id: str, at: datetime) -> int:
        """Contacts inside the rolling window ending at ``at``."""
        window_start = at - timedelta(hours=self.policy.contact_window_hours)
        stamps = self.contacts_by_customer.get(customer_id, ())
        return sum(1 for s in stamps if s > window_start)

    def can_afford(self, cost: float) -> bool:
        return cost <= self.remaining_budget + 1e-9

    # -- writes ------------------------------------------------------------

    def reserve(
        self,
        item_id: str,
        customer_id: str,
        action: ActionType,
        at: datetime,
        cost: float,
    ) -> None:
        """Commit budget and cap headroom at decision time.

        Reservation happens when the desk decides, not when it dispatches,
        because a budget that is only debited on dispatch lets the allocator
        promise the same rupee to two items.
        """
        self.spend += cost
        if action in RETRY_ACTIONS:
            self.retries_by_item[item_id] = self.retries_for(item_id) + 1
        if action in CONTACT_ACTIONS:
            self.contacts_by_customer.setdefault(customer_id, []).append(at)

    def claim(self, key: str) -> bool:
        """Take an idempotency key. False means this action already happened.

        A duplicate reaching this point is a correctness failure, not a cost
        overrun: it is counted as a policy violation and refused.
        """
        if key in self.seen_keys:
            self.violations += 1
            return False
        self.seen_keys.add(key)
        return True

    def halt(self) -> None:
        """Kill switch. Stops dispatch mid-batch and leaves the ledger consistent."""
        self.halted = True
