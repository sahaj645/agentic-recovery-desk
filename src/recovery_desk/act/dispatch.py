"""Stage 4. Dispatch the chosen action idempotently and record what happened.

Dry-run is the default and live dispatch does not exist in this build: there is
no code path from a decision to a real debit or a real message. That is a
deliberate bound, not an unfinished feature. What the simulator provides is the
*outcome*; everything about how the action was authorised, keyed, capped and
logged is real.
"""

from __future__ import annotations

from typing import Sequence

from ..fixtures.world import World
from ..models import (
    ActionAttempt,
    ActionType,
    AtRiskItem,
    Decision,
    DecisionStatus,
    Outcome,
)
from .ledger import Ledger, idempotency_key


def dispatch(
    decisions: Sequence[Decision],
    items: dict[str, AtRiskItem],
    world: World,
    ledger: Ledger,
    run_id: str,
    dry_run: bool = True,
) -> list[ActionAttempt]:
    """Turn decisions into attempts, one idempotency key at a time."""
    attempts: list[ActionAttempt] = []

    for decision in decisions:
        item = items[decision.item_id]

        if (
            decision.status is DecisionStatus.SUPPRESSED
            or decision.chosen_action in (None, ActionType.DO_NOTHING)
        ):
            attempts.append(
                ActionAttempt(
                    decision_id=decision.decision_id,
                    item_id=item.id,
                    idempotency_key="",
                    action_type=ActionType.DO_NOTHING,
                    dispatched_at=None,
                    outcome=Outcome.NOT_DISPATCHED,
                    amount_recovered=0.0,
                    cost_incurred=0.0,
                    dry_run=dry_run,
                )
            )
            continue

        if ledger.halted:
            # The kill switch stops dispatch mid-batch and leaves everything
            # already written consistent. Remaining items are simply not sent.
            attempts.append(
                ActionAttempt(
                    decision_id=decision.decision_id,
                    item_id=item.id,
                    idempotency_key="",
                    action_type=ActionType.DO_NOTHING,
                    dispatched_at=None,
                    outcome=Outcome.NOT_DISPATCHED,
                    amount_recovered=0.0,
                    cost_incurred=0.0,
                    dry_run=dry_run,
                )
            )
            continue

        action = decision.chosen_action
        key = idempotency_key(run_id, item.id, action, decision.attempt_index)

        if not ledger.claim(key):
            # Replay protection. The spend was already reserved and the action
            # already happened; doing it again is the double-charge this key exists
            # to prevent.
            attempts.append(
                ActionAttempt(
                    decision_id=decision.decision_id,
                    item_id=item.id,
                    idempotency_key=key,
                    action_type=action,
                    dispatched_at=None,
                    outcome=Outcome.NOT_DISPATCHED,
                    amount_recovered=0.0,
                    cost_incurred=0.0,
                    dry_run=dry_run,
                )
            )
            continue

        executed_at = decision.scheduled_for or item.occurred_at
        recovered = world.resolve(item.id, action, executed_at)

        attempts.append(
            ActionAttempt(
                decision_id=decision.decision_id,
                item_id=item.id,
                idempotency_key=key,
                action_type=action,
                dispatched_at=executed_at,
                outcome=Outcome.RECOVERED if recovered else Outcome.NOT_RECOVERED,
                amount_recovered=item.amount if recovered else 0.0,
                cost_incurred=decision.estimated_cost,
                dry_run=dry_run,
            )
        )

    return attempts
