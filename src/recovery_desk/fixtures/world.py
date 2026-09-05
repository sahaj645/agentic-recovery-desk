"""The outcome oracle: what would actually have happened.

THE CIRCULARITY FIREWALL — the single most important property in this file.

The desk must never be able to read the variables this module uses to decide
outcomes. If it could, a good score would prove nothing except that the desk
and the simulator share a spreadsheet.

    Latent (simulator only)          Observable (desk only)
    ------------------------         ----------------------------
    true_class                       raw_gateway_context (noisy text)
    balance_recovers_at              amount, occurred_at
    issuer_outage_ends_at            prior_attempts, prior_contacts
    customer_patience                customer_id, merchant_id
    rail_affinity
    draws

The two sets are disjoint, and the ``test_circularity_firewall`` invariant in
``tests/test_invariants.py`` asserts it against the actual dataclass fields
rather than against a comment.

The desk can observe the *calendar* — it knows when Indian salary credits
typically land. It cannot observe ``balance_recovers_at``, which is drawn with
jitter around that calendar. That gap is exactly where the salary-cycle rule
earns its keep, or fails to.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, fields
from datetime import datetime, timedelta

from ..calendar_facts import next_salary_credit
from ..models import ActionType, AtRiskItem, FailureClass

#: Outcomes are drawn per *family*, not per action. An item is recoverable or
#: not largely as a property of itself: if the customer has no funds, no retry
#: works regardless of which rail it goes down, and if they have no intent, no
#: channel reaches them. Drawing independently per action would hand every item
#: six lottery tickets and quietly inflate the recoverable denominator.
ACTION_FAMILY: dict[ActionType, str] = {
    ActionType.RETRY_NOW: "retry",
    ActionType.RETRY_SCHEDULED: "retry",
    ActionType.ALTERNATE_RAIL: "retry",
    ActionType.NUDGE_SMS: "contact",
    ActionType.NUDGE_WHATSAPP: "contact",
    ActionType.VOICE_CALL: "contact",
}


@dataclass(frozen=True, slots=True)
class Latent:
    """Ground-truth state of one item. Never crosses into the desk."""

    item_id: str
    true_class: FailureClass
    balance_recovers_at: datetime | None
    issuer_outage_ends_at: datetime | None
    customer_patience: float
    rail_affinity: float
    draws: dict[str, float]


def latent_field_names() -> frozenset[str]:
    return frozenset(f.name for f in fields(Latent)) - {"item_id"}


def observable_field_names() -> frozenset[str]:
    return frozenset(f.name for f in fields(AtRiskItem)) - {"id"}


class World:
    """Resolves an action into an outcome. Deterministic given the fixture seed.

    Each item carries one pre-drawn uniform per action family, fixed at
    generation time. Resolution is a threshold comparison against that draw,
    so replaying a batch — or running five baselines over the same batch —
    sees exactly the same world.
    """

    def __init__(self, latents: dict[str, Latent]) -> None:
        self.latents = latents

    # -- outcome probability ----------------------------------------------

    def success_probability(
        self, latent: Latent, action: ActionType, executed_at: datetime
    ) -> float:
        if action is ActionType.DO_NOTHING:
            return 0.0

        cls = latent.true_class
        is_retry = action in (
            ActionType.RETRY_NOW,
            ActionType.RETRY_SCHEDULED,
        )
        is_contact = action in (
            ActionType.NUDGE_SMS,
            ActionType.NUDGE_WHATSAPP,
            ActionType.VOICE_CALL,
        )
        contact_strength = {
            ActionType.NUDGE_SMS: 0.7,
            ActionType.NUDGE_WHATSAPP: 1.0,
            ActionType.VOICE_CALL: 1.25,
        }.get(action, 0.0)

        if cls is FailureClass.ACCOUNT_BLOCKED:
            # Nothing works. Spending here is pure waste, by construction.
            return 0.0

        if cls is FailureClass.BANK_TIMEOUT:
            if action is ActionType.ALTERNATE_RAIL:
                # A different rail sidesteps the outage entirely.
                return 0.35 + 0.40 * latent.rail_affinity
            if is_retry:
                still_down = (
                    latent.issuer_outage_ends_at is not None
                    and executed_at < latent.issuer_outage_ends_at
                )
                return 0.05 if still_down else 0.58
            if is_contact:
                return (0.04 + 0.08 * latent.customer_patience) * contact_strength
            return 0.0

        if cls is FailureClass.NETWORK:
            if action is ActionType.ALTERNATE_RAIL:
                return 0.30 + 0.25 * latent.rail_affinity
            if is_retry:
                # A short delay is enough; an immediate re-present often is not.
                waited = latent.issuer_outage_ends_at is None or (
                    executed_at >= latent.issuer_outage_ends_at
                )
                return 0.46 if waited else 0.22
            if is_contact:
                return (0.05 + 0.08 * latent.customer_patience) * contact_strength
            return 0.0

        if cls is FailureClass.INSUFFICIENT_BALANCE:
            if is_retry or action is ActionType.ALTERNATE_RAIL:
                # Balance is a calendar phenomenon. Timing is the whole game.
                if latent.balance_recovers_at is None:
                    return 0.04
                return 0.74 if executed_at >= latent.balance_recovers_at else 0.04
            if is_contact:
                return (0.08 + 0.24 * latent.customer_patience) * contact_strength
            return 0.0

        if cls is FailureClass.WRONG_PIN:
            if is_retry or action is ActionType.ALTERNATE_RAIL:
                # Re-presenting the same debit against the same credential fails.
                return 0.10 if action is ActionType.ALTERNATE_RAIL else 0.03
            if is_contact:
                # Offering another method is the only thing that works here.
                return (0.10 + 0.34 * latent.customer_patience) * contact_strength
            return 0.0

        return 0.05

    # -- resolution --------------------------------------------------------

    def resolve(self, item_id: str, action: ActionType, executed_at: datetime) -> bool:
        latent = self.latents[item_id]
        probability = self.success_probability(latent, action, executed_at)
        return latent.draws[ACTION_FAMILY[action]] < probability

    def oracle_best_time(self, latent: Latent, action: ActionType) -> datetime:
        """The most favourable moment this action could have been taken.

        Used only to compute the ground-truth denominator — what was genuinely
        recoverable — never to make a decision.
        """
        candidates = [latent.balance_recovers_at, latent.issuer_outage_ends_at]
        best = max([c for c in candidates if c is not None], default=None)
        if best is None:
            return datetime(2026, 1, 1) + timedelta(days=365)
        return best + timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """What each action would have achieved under perfect timing."""

    item_id: str
    amount: float
    true_class: FailureClass
    recoverable_by: dict[str, bool]

    @property
    def is_recoverable(self) -> bool:
        return any(self.recoverable_by.values())


def build_ground_truth(
    items: list[AtRiskItem], world: World
) -> dict[str, GroundTruth]:
    truth: dict[str, GroundTruth] = {}
    for item in items:
        latent = world.latents[item.id]
        recoverable_by = {}
        for action in ActionType:
            if action is ActionType.DO_NOTHING:
                continue
            best_time = world.oracle_best_time(latent, action)
            recoverable_by[action.value] = world.resolve(item.id, action, best_time)
        truth[item.id] = GroundTruth(
            item_id=item.id,
            amount=item.amount,
            true_class=latent.true_class,
            recoverable_by=recoverable_by,
        )
    return truth


def draw_latent(rng: random.Random, item_id: str, true_class: FailureClass,
                occurred_at: datetime) -> Latent:
    balance_recovers_at = None
    issuer_outage_ends_at = None

    if true_class is FailureClass.INSUFFICIENT_BALANCE:
        payday = next_salary_credit(occurred_at)
        # Jitter: the desk knows the calendar, not the individual's actual credit.
        balance_recovers_at = payday + timedelta(hours=rng.gauss(0, 30))

    if true_class in (FailureClass.BANK_TIMEOUT, FailureClass.NETWORK):
        issuer_outage_ends_at = occurred_at + timedelta(
            minutes=rng.choice([12, 25, 45, 90, 180, 240])
        )

    return Latent(
        item_id=item_id,
        true_class=true_class,
        balance_recovers_at=balance_recovers_at,
        issuer_outage_ends_at=issuer_outage_ends_at,
        customer_patience=rng.betavariate(2.2, 2.6),
        rail_affinity=rng.betavariate(2.5, 2.0),
        draws={"retry": rng.random(), "contact": rng.random()},
    )
