"""The pricing of a decision.

    EV(item, action) = P(recover | item, action) x amount x margin
                     - cost(action)
                     - lambda x contact_fatigue(customer, action)

Every term is computed here and kept, never collapsed into a single number
before it reaches the audit log. The reason item 447 was chosen and item 448
was not has to be readable line by line, and that is only possible if the
arithmetic survives the trip.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..calendar_facts import next_salary_credit
from ..config import Policy
from ..diagnose import priors
from ..models import (
    CONTACT_ACTIONS,
    ActionCandidate,
    ActionType,
    AtRiskItem,
    Diagnosis,
    EVBreakdown,
    FailureClass,
)

#: How long the desk waits before each action, when no smarter rule applies.
BASE_DELAYS: dict[ActionType, timedelta] = {
    ActionType.RETRY_NOW: timedelta(minutes=15),
    ActionType.RETRY_SCHEDULED: timedelta(hours=4),
    ActionType.ALTERNATE_RAIL: timedelta(minutes=30),
    ActionType.NUDGE_SMS: timedelta(hours=1),
    ActionType.NUDGE_WHATSAPP: timedelta(hours=1),
    ActionType.VOICE_CALL: timedelta(hours=2),
}

#: Issuer maintenance hours. Re-presenting a debit inside this window is worth
#: less, and the desk knows it without having to be told by a model.
QUIET_HOURS = range(2, 5)


def schedule(item: AtRiskItem, diagnosis: Diagnosis, action: ActionType) -> datetime:
    """When the desk would take this action.

    The salary-cycle rule lives here, and it is the sharpest rule in the system:
    an insufficient-balance failure is not a backoff problem, it is a calendar
    problem. Scheduling the retry against the salary credit rather than against
    an exponential curve is the single behaviour that should separate this desk
    from a blanket retry loop.
    """
    if action is ActionType.DO_NOTHING:
        return item.occurred_at

    if (
        action is ActionType.RETRY_SCHEDULED
        and diagnosis.failure_class is FailureClass.INSUFFICIENT_BALANCE
    ):
        # Land just after the credit, not on the hour everyone else retries.
        return next_salary_credit(item.occurred_at) + timedelta(hours=3)

    return item.occurred_at + BASE_DELAYS[action]


def probability(
    item: AtRiskItem, diagnosis: Diagnosis, action: ActionType, executed_at: datetime
) -> float:
    """Published base rate as the prior; deterministic context adjustments on top.

    This is the "Both" row of the ownership table: the model contributes the
    failure class, the rules contribute everything that happens to the number
    afterwards. No sampler touches the arithmetic.
    """
    if action is ActionType.DO_NOTHING:
        return 0.0

    base = priors.prior_for(diagnosis.failure_class, action)

    # A low-confidence diagnosis is blended toward the pessimistic unknown prior
    # rather than trusted outright. Uncertainty should cost money, not earn
    # optimism.
    unknown = priors.prior_for(FailureClass.UNKNOWN, action)
    confidence = max(0.0, min(1.0, diagnosis.confidence))
    p = confidence * base + (1.0 - confidence) * unknown

    # Each previous failed attempt on this item is evidence against the next one.
    p *= 0.75 ** item.prior_attempts

    # Re-presenting a debit during issuer maintenance hours is worth less.
    if action in (ActionType.RETRY_NOW, ActionType.RETRY_SCHEDULED):
        if executed_at.hour in QUIET_HOURS:
            p *= 0.85

    # A retry timed to the salary credit is the reason the scheduled action
    # exists. The desk believes in its own rule, and the harness checks it.
    if (
        action is ActionType.RETRY_SCHEDULED
        and diagnosis.failure_class is FailureClass.INSUFFICIENT_BALANCE
        and executed_at >= next_salary_credit(item.occurred_at)
    ):
        p = max(p, 0.62)

    # Very large tickets clear slightly less often on a re-presented debit.
    if item.amount > 25_000 and action in (
        ActionType.RETRY_NOW,
        ActionType.RETRY_SCHEDULED,
    ):
        p *= 0.90

    return max(0.0, min(1.0, p))


def contact_fatigue(item: AtRiskItem, action: ActionType, contacts_so_far: int) -> float:
    """Fatigue units for touching this customer once more.

    A retry is invisible to the customer and costs no goodwill. A message or a
    call does, and the third one costs far more than the first -- so the penalty
    is superlinear in touches already made.

    The first contact carries zero units on purpose. One message about a failed
    payment is service, not harassment, and charging goodwill for it prices the
    desk out of the wrong-PIN pool, which is the one class that can *only* be
    recovered by reaching the customer. The second touch costs lambda, the third
    costs four times that, and the cap stops it long before the sixth.
    """
    if action not in CONTACT_ACTIONS:
        return 0.0
    touches = item.prior_contacts + contacts_so_far
    return float(touches ** 2)


def evaluate(
    item: AtRiskItem,
    diagnosis: Diagnosis,
    action: ActionType,
    policy: Policy,
    contacts_so_far: int = 0,
) -> tuple[ActionCandidate, EVBreakdown]:
    executed_at = schedule(item, diagnosis, action)
    p = probability(item, diagnosis, action, executed_at)
    cost = policy.cost_of(action)
    units = contact_fatigue(item, action, contacts_so_far)
    penalty = policy.lambda_fatigue * units
    gross = p * item.amount * policy.margin

    candidate = ActionCandidate(
        item_id=item.id,
        action_type=action,
        estimated_cost=cost,
        estimated_p_recover=p,
        earliest_executable_at=executed_at,
    )
    breakdown = EVBreakdown(
        p_recover=p,
        amount=item.amount,
        margin=policy.margin,
        gross_value=gross,
        cost=cost,
        fatigue_units=units,
        fatigue_penalty=penalty,
        ev=gross - cost - penalty,
    )
    return candidate, breakdown


def evaluate_all(
    item: AtRiskItem,
    diagnosis: Diagnosis,
    policy: Policy,
    contacts_so_far: int = 0,
) -> list[tuple[ActionCandidate, EVBreakdown]]:
    """Price every permitted action, including doing nothing.

    "Do nothing" is a first-class action with cost zero and EV zero. Any item
    whose best action scores below it is suppressed, and that suppression is a
    result the desk reports rather than a gap it hides.
    """
    return [
        evaluate(item, diagnosis, action, policy, contacts_so_far)
        for action in ActionType
    ]
