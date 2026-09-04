"""The nine reported numbers.

Two of them are the ones nobody else reports. ``wasted_spend`` is money burned
on items that were never going to recover, and ``contacts_per_rupee_recovered``
is the customer's patience priced as a resource. A recovery agent that reports
only what it recovered is reporting half the ledger.

``policy_violations`` is not a metric to be improved. Any non-zero value is a
build failure.
"""

from __future__ import annotations

from typing import Sequence

from ..act.ledger import Ledger
from ..config import Policy
from ..fixtures.world import GroundTruth
from ..models import (
    CONTACT_ACTIONS,
    ActionAttempt,
    AtRiskItem,
    Decision,
    DecisionStatus,
    Metrics,
    Outcome,
)


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def compute(
    items: Sequence[AtRiskItem],
    decisions: Sequence[Decision],
    attempts: Sequence[ActionAttempt],
    ground_truth: dict[str, GroundTruth],
    ledger: Ledger,
    latencies: Sequence[float],
    policy: Policy,
) -> Metrics:
    amounts = {item.id: item.amount for item in items}

    # An item is recovered once, however many attempts it took.
    recovered_ids = {
        a.item_id for a in attempts if a.outcome is Outcome.RECOVERED
    }
    gross_recovered = sum(amounts[i] for i in recovered_ids)

    total_spend = sum(a.cost_incurred for a in attempts)
    net_recovered = gross_recovered - total_spend

    recoverable_value = sum(
        t.amount for t in ground_truth.values() if t.is_recoverable
    )
    recovery_rate = gross_recovered / recoverable_value if recoverable_value else 0.0

    chased_ids = {
        d.item_id for d in decisions if d.status is DecisionStatus.CHASE
    }
    suppressed_ids = {i.id for i in items} - chased_ids

    # Spend on items that never recovered, whatever the reason.
    wasted_spend = sum(
        a.cost_incurred for a in attempts if a.item_id not in recovered_ids
    )

    # Precision is measured against what was *genuinely* recoverable, not
    # against what happened to work: chasing a recoverable item and missing it
    # is a timing failure, not a targeting failure.
    truly_recoverable = {
        item_id for item_id, t in ground_truth.items() if t.is_recoverable
    }
    chase_precision = (
        len(chased_ids & truly_recoverable) / len(chased_ids) if chased_ids else 0.0
    )

    contacts_made = sum(
        1
        for a in attempts
        if a.action_type in CONTACT_ACTIONS and a.dispatched_at is not None
    )

    contacts_per_rupee = contacts_made / gross_recovered if gross_recovered else 0.0
    cost_per_rupee = total_spend / gross_recovered if gross_recovered else 0.0

    violations = ledger.violations
    if ledger.spend > policy.budget + 1e-6:
        # Reaching here means a ceiling was checked after a dispatch rather than
        # before one. It is a correctness bug, and it is counted as such.
        violations += 1

    return Metrics(
        gross_recovered=gross_recovered,
        net_recovered=net_recovered,
        recovery_rate=recovery_rate,
        wasted_spend=wasted_spend,
        chase_precision=chase_precision,
        contacts_per_rupee_recovered=contacts_per_rupee,
        cost_per_rupee_recovered=cost_per_rupee,
        p95_decision_latency_ms=percentile(latencies, 95),
        policy_violations=violations,
        items_total=len(items),
        items_chased=len(chased_ids),
        items_suppressed=len(suppressed_ids),
        items_recovered=len(recovered_ids),
        recoverable_value=recoverable_value,
        total_spend=total_spend,
        contacts_made=contacts_made,
    )
