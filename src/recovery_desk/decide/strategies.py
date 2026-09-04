"""The arms of the evaluation: four policies over one identical world.

Every arm gets the same fixture, the same seed, the same budget and the same
ledger discipline. Only the allocation policy differs, which is what makes the
comparison mean anything.

    B0  do nothing                 the floor: how much is genuinely at stake
    B1  blanket retry x3           what most merchants actually do
    B2  rules-only heuristic       the best static policy with no model at all
    B3* Recovery Desk, no model    EV allocation on the deterministic classifier
    B3  Recovery Desk              EV allocation on the model classifier

B2 is the honest bar. B3* exists so the model's contribution can be stated as a
number -- B3 minus B3* -- instead of asserted. If B2 captures most of B3's
result, that is the finding, and it gets reported.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, Sequence

from ..act.ledger import Ledger
from ..config import Policy
from ..diagnose import priors
from ..models import (
    ActionType,
    AtRiskItem,
    Decision,
    DecisionStatus,
    Diagnosis,
    FailureClass,
    SuppressionReason,
)
from .allocator import allocate
from .policy import evaluate_gate

#: B1's fixed backoff curve. Three attempts, evenly spaced on a clock, with no
#: reference to why the payment failed or when the customer might have money.
BLANKET_BACKOFF = (timedelta(minutes=30), timedelta(hours=2), timedelta(hours=8))

#: B2 static map: one action and one fixed delay per failure class, no
#: arithmetic anywhere. This is deliberately the *strongest* policy available
#: without a model or an optimiser, because a weak B2 would make the desk look
#: good for free. It already knows the three things a competent engineer knows:
#: a blocked account is hopeless, a wrong PIN wants a different method rather
#: than another debit, and a technical decline should be given time to clear
#: rather than re-presented immediately.
#:
#: What it does not have is the salary-cycle timing, the rail choice, or any
#: notion of what an action is worth. That gap is what the harness measures.
STATIC_ACTION: dict[FailureClass, tuple[ActionType, timedelta] | None] = {
    FailureClass.BANK_TIMEOUT: (ActionType.RETRY_SCHEDULED, timedelta(hours=4)),
    FailureClass.NETWORK: (ActionType.RETRY_SCHEDULED, timedelta(hours=4)),
    FailureClass.INSUFFICIENT_BALANCE: (ActionType.RETRY_SCHEDULED, timedelta(hours=24)),
    FailureClass.WRONG_PIN: (ActionType.NUDGE_SMS, timedelta(hours=1)),
    FailureClass.ACCOUNT_BLOCKED: None,
    FailureClass.UNKNOWN: (ActionType.RETRY_SCHEDULED, timedelta(hours=4)),
}


class Strategy(Protocol):
    """An arm plans in waves, and only ever sees items that are still unrecovered.

    Waves exist because dunning is not a single shot: a merchant retrying three
    times stops as soon as one succeeds. Handing every arm the same wave loop --
    and passing it only the still-pending items -- is what stops the comparison
    from charging B1 for retries a real merchant would never have sent.
    """

    id: str
    name: str
    description: str
    max_rounds: int

    def plan(
        self,
        round_index: int,
        items: Sequence[AtRiskItem],
        diagnoses: dict[str, Diagnosis],
        policy: Policy,
        ledger: Ledger,
        run_id: str,
    ) -> tuple[list[Decision], list[float]]: ...


def _decision(
    run_id: str,
    item: AtRiskItem,
    action: ActionType | None,
    scheduled_for,
    cost: float,
    rationale: str,
    provenance: str,
    reason: SuppressionReason | None = None,
    attempt_index: int = 0,
    checks: tuple = (),
) -> Decision:
    chasing = action is not None and action is not ActionType.DO_NOTHING
    return Decision(
        decision_id="%s:%s:%d" % (run_id, item.id, attempt_index),
        item_id=item.id,
        status=DecisionStatus.CHASE if chasing else DecisionStatus.SUPPRESSED,
        chosen_action=action or ActionType.DO_NOTHING,
        ev=0.0,
        # Baselines carry no EV table because they do no EV arithmetic. The
        # empty table is the honest representation of that, not a gap.
        ev_table=(),
        rationale=rationale,
        provenance=provenance,
        policy_checks=checks,
        scheduled_for=scheduled_for if chasing else None,
        estimated_cost=cost if chasing else 0.0,
        suppression_reason=reason,
        attempt_index=attempt_index,
    )


@dataclass(frozen=True, slots=True)
class DoNothing:
    """B0. The floor. Establishes how much revenue is genuinely at stake."""

    id: str = "B0"
    name: str = "Do nothing"
    description: str = "No action on any item. The revenue floor."
    max_rounds: int = 1

    def plan(self, round_index, items, diagnoses, policy, ledger, run_id):
        started = time.perf_counter_ns()
        decisions = [
            _decision(
                run_id,
                item,
                None,
                None,
                0.0,
                "Baseline B0 takes no action on any item.",
                "baseline:B0",
                SuppressionReason.BASELINE_NO_ACTION,
            )
            for item in items
        ]
        per_item = (time.perf_counter_ns() - started) / 1e6 / max(len(decisions), 1)
        return decisions, [per_item] * len(decisions)


@dataclass(frozen=True, slots=True)
class BlanketRetry:
    """B1. Retry everything three times on a fixed backoff, oldest first.

    No triage, no failure classification, no notion of which failures can never
    recover. It spends the same budget as every other arm, and where it spends
    it is the entire point of the comparison.
    """

    id: str = "B1"
    name: str = "Blanket retry x3"
    description: str = "Retry every item up to three times on a fixed backoff, FIFO."
    max_rounds: int = len(BLANKET_BACKOFF)

    def plan(self, round_index, items, diagnoses, policy, ledger, run_id):
        decisions: list[Decision] = []
        elapsed: list[float] = []
        cost = policy.cost_of(ActionType.RETRY_NOW)
        delay = BLANKET_BACKOFF[round_index]

        for item in sorted(items, key=lambda i: i.occurred_at):
            started = time.perf_counter_ns()
            if not ledger.can_afford(cost):
                decisions.append(
                    _decision(
                        run_id, item, None, None, 0.0,
                        "Budget exhausted before this attempt.",
                        "baseline:B1",
                        SuppressionReason.BUDGET_EXHAUSTED,
                        attempt_index=round_index,
                    )
                )
                elapsed.append((time.perf_counter_ns() - started) / 1e6)
                continue
            ledger.reserve(
                item_id=item.id,
                customer_id=item.customer_id,
                action=ActionType.RETRY_NOW,
                at=item.occurred_at + delay,
                cost=cost,
            )
            decisions.append(
                _decision(
                    run_id,
                    item,
                    ActionType.RETRY_NOW,
                    item.occurred_at + delay,
                    cost,
                    "Blanket retry attempt %d of %d on a fixed %s backoff."
                    % (round_index + 1, len(BLANKET_BACKOFF), delay),
                    "baseline:B1",
                    attempt_index=round_index,
                )
            )
            elapsed.append((time.perf_counter_ns() - started) / 1e6)
        return decisions, elapsed


@dataclass(frozen=True, slots=True)
class RulesOnly:
    """B2. One fixed action per failure class, largest amounts first.

    This is the strongest policy available without any model and without any
    expected-value arithmetic: it knows blocked accounts are hopeless and that a
    wrong PIN wants a different method rather than another debit. What it does
    not do is price anything.
    """

    id: str = "B2"
    name: str = "Rules-only heuristic"
    description: str = (
        "Static action per failure class, ranked by amount, capped by budget."
    )
    max_rounds: int = 1

    def plan(self, round_index, items, diagnoses, policy, ledger, run_id):
        decisions: list[Decision] = []
        elapsed: list[float] = []
        ordered = sorted(items, key=lambda i: i.amount, reverse=True)

        for item in ordered:
            started = time.perf_counter_ns()
            diagnosis = diagnoses[item.id]
            rule = STATIC_ACTION[diagnosis.failure_class]

            if rule is None:
                decisions.append(
                    _decision(
                        run_id, item, None, None, 0.0,
                        "Static rule: %s is never chased."
                        % diagnosis.failure_class.value,
                        diagnosis.classifier_provenance,
                        SuppressionReason.UNRECOVERABLE_CLASS,
                    )
                )
                elapsed.append((time.perf_counter_ns() - started) / 1e6)
                continue

            # A fixed clock delay, the same for every item in the class. The
            # salary-cycle rule belongs to the desk, not to the baseline.
            action, delay = rule
            scheduled_for = item.occurred_at + delay
            cost = policy.cost_of(action)
            gate = evaluate_gate(
                item, diagnosis, action, cost, scheduled_for, policy, ledger
            )
            if not gate.passed:
                decisions.append(
                    _decision(
                        run_id, item, None, None, 0.0,
                        "Static rule proposed %s; policy gate refused it (%s)."
                        % (action.value, gate.reason.value),
                        diagnosis.classifier_provenance,
                        gate.reason,
                        checks=tuple(gate.checks),
                    )
                )
                elapsed.append((time.perf_counter_ns() - started) / 1e6)
                continue

            ledger.reserve(
                item_id=item.id,
                customer_id=item.customer_id,
                action=action,
                at=scheduled_for,
                cost=cost,
            )
            decisions.append(
                _decision(
                    run_id, item, action, scheduled_for, cost,
                    "Static rule for %s: %s."
                    % (diagnosis.failure_class.value, action.value),
                    diagnosis.classifier_provenance,
                    checks=tuple(gate.checks),
                )
            )
            elapsed.append((time.perf_counter_ns() - started) / 1e6)

        by_id = {d.item_id: d for d in decisions}
        return [by_id[i.id] for i in items], elapsed


@dataclass(frozen=True, slots=True)
class RecoveryDesk:
    """B3. Expected-value allocation under budget, caps and the policy gate.

    The arm id and name are supplied by the harness so the same engine can run
    twice -- once on the deterministic classifier, once on the model -- with
    nothing else differing between the two.
    """

    id: str = "B3"
    name: str = "Recovery Desk"
    description: str = "EV-ranked allocation of a finite budget, greedy per rupee."
    max_rounds: int = 1

    def plan(self, round_index, items, diagnoses, policy, ledger, run_id):
        return allocate(list(items), diagnoses, policy, ledger, run_id)


def unrecoverable_classes() -> frozenset[FailureClass]:
    return priors.UNRECOVERABLE_CLASSES
