"""Stage 3. Allocate a finite budget across a heterogeneous pool.

Selection is greedy on EV-per-rupee-spent. That is a deliberate choice over a
fancier optimiser: greedy is O(n log n), it is explainable line by line, and it
produces a decision trace a human can audit. A solver would buy a small amount
of optimality at the cost of the only property that matters here -- being able
to say exactly why item 447 was chosen and item 448 was not.

The pass structure matters as much as the ranking:

    Pass A  price every action for every item, discard anything the gate
            refuses outright, and drop items whose best action loses money
    Pass B  walk the survivors in EV-per-rupee order, re-pricing against the
            live ledger, committing until the budget or a cap binds

Pass B re-prices rather than trusting Pass A because contact fatigue and the
remaining budget both move as the desk commits. Ranking on a stale number is
acceptable; *spending* on one is not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..act.ledger import Ledger
from ..config import Policy
from ..models import (
    ActionType,
    AtRiskItem,
    CandidateEvaluation,
    Decision,
    DecisionStatus,
    Diagnosis,
    EVBreakdown,
    PolicyCheck,
    SuppressionReason,
)
from . import ev as ev_module
from .policy import evaluate_gate


@dataclass(slots=True)
class _Scored:
    item: AtRiskItem
    diagnosis: Diagnosis
    table: list[CandidateEvaluation]
    best: CandidateEvaluation | None
    rank_key: float


def _price_item(
    item: AtRiskItem,
    diagnosis: Diagnosis,
    policy: Policy,
    ledger: Ledger,
) -> tuple[list[CandidateEvaluation], CandidateEvaluation | None]:
    """Price every action once, marking which ones the gate would refuse."""
    table: list[CandidateEvaluation] = []
    best: CandidateEvaluation | None = None

    for candidate, breakdown in ev_module.evaluate_all(item, diagnosis, policy):
        if candidate.action_type is ActionType.DO_NOTHING:
            table.append(
                CandidateEvaluation(
                    candidate=candidate, breakdown=breakdown, eligible=True
                )
            )
            continue

        gate = evaluate_gate(
            item=item,
            diagnosis=diagnosis,
            action=candidate.action_type,
            cost=candidate.estimated_cost,
            executed_at=candidate.earliest_executable_at,
            policy=policy,
            ledger=ledger,
        )
        # Budget is a batch-level constraint resolved in Pass B, not a property
        # of the action, so it does not disqualify a candidate from the table.
        refused = gate.reason is not None and (
            gate.reason is not SuppressionReason.BUDGET_EXHAUSTED
        )
        evaluation = CandidateEvaluation(
            candidate=candidate,
            breakdown=breakdown,
            eligible=not refused,
            block_reason=gate.reason.value if refused else None,
        )
        table.append(evaluation)

        if not refused and breakdown.ev > 0:
            if best is None or breakdown.ev > best.breakdown.ev:
                best = evaluation

    return table, best


def _suppress(
    run_id: str,
    item: AtRiskItem,
    table: list[CandidateEvaluation],
    reason: SuppressionReason,
    rationale: str,
    provenance: str,
    checks: tuple[PolicyCheck, ...] = (),
) -> Decision:
    return Decision(
        decision_id="%s:%s" % (run_id, item.id),
        item_id=item.id,
        status=DecisionStatus.SUPPRESSED,
        chosen_action=ActionType.DO_NOTHING,
        ev=0.0,
        ev_table=tuple(table),
        rationale=rationale,
        provenance=provenance,
        policy_checks=checks,
        suppression_reason=reason,
    )


def allocate(
    items: list[AtRiskItem],
    diagnoses: dict[str, Diagnosis],
    policy: Policy,
    ledger: Ledger,
    run_id: str,
) -> tuple[list[Decision], list[float]]:
    """Return one decision per item, plus per-item decision latencies in ms."""
    decisions: dict[str, Decision] = {}
    elapsed_ms: dict[str, float] = {}
    scored: list[_Scored] = []

    # -- Pass A: price everything ----------------------------------------
    for item in items:
        started = time.perf_counter_ns()
        diagnosis = diagnoses[item.id]
        table, best = _price_item(item, diagnosis, policy, ledger)
        elapsed_ms[item.id] = (time.perf_counter_ns() - started) / 1e6

        if best is None:
            blocked = [e for e in table if e.block_reason]
            unrecoverable = any(
                e.block_reason == SuppressionReason.UNRECOVERABLE_CLASS.value
                for e in blocked
            )
            if unrecoverable:
                reason = SuppressionReason.UNRECOVERABLE_CLASS
                rationale = (
                    "Not worth chasing: the issuer has blocked or frozen this "
                    "account, so no action has any expected recovery."
                )
            else:
                reason = SuppressionReason.NEGATIVE_EV
                top = max(table, key=lambda e: e.breakdown.ev)
                rationale = (
                    "Not worth chasing: the best available action (%s) is worth "
                    "%.2f after cost and contact fatigue, which is below doing "
                    "nothing." % (top.candidate.action_type.value, top.breakdown.ev)
                )
            decisions[item.id] = _suppress(
                run_id, item, table, reason, rationale, diagnosis.classifier_provenance
            )
            continue

        scored.append(
            _Scored(
                item=item,
                diagnosis=diagnosis,
                table=table,
                best=best,
                rank_key=best.breakdown.ev_per_rupee,
            )
        )

    # -- Pass B: spend, best value per rupee first ------------------------
    scored.sort(key=lambda s: s.rank_key, reverse=True)

    for entry in scored:
        item, diagnosis = entry.item, entry.diagnosis
        started = time.perf_counter_ns()

        # Re-price against the live ledger: fatigue and budget have moved.
        contacts = ledger.contacts_for(
            item.customer_id, entry.best.candidate.earliest_executable_at
        )
        table: list[CandidateEvaluation] = []
        chosen: CandidateEvaluation | None = None
        chosen_checks: tuple[PolicyCheck, ...] = ()
        blocking: SuppressionReason | None = None
        blocking_checks: tuple[PolicyCheck, ...] = ()

        for candidate, breakdown in ev_module.evaluate_all(
            item, diagnosis, policy, contacts_so_far=contacts
        ):
            if candidate.action_type is ActionType.DO_NOTHING:
                table.append(
                    CandidateEvaluation(
                        candidate=candidate, breakdown=breakdown, eligible=True
                    )
                )
                continue
            gate = evaluate_gate(
                item=item,
                diagnosis=diagnosis,
                action=candidate.action_type,
                cost=candidate.estimated_cost,
                executed_at=candidate.earliest_executable_at,
                policy=policy,
                ledger=ledger,
            )
            table.append(
                CandidateEvaluation(
                    candidate=candidate,
                    breakdown=breakdown,
                    eligible=gate.passed,
                    block_reason=gate.reason.value if gate.reason else None,
                )
            )
            if gate.passed and breakdown.ev > 0:
                if chosen is None or breakdown.ev > chosen.breakdown.ev:
                    chosen = table[-1]
                    chosen_checks = tuple(gate.checks)
            elif gate.reason is not None and blocking is None:
                blocking = gate.reason
                blocking_checks = tuple(gate.checks)

        elapsed_ms[item.id] += (time.perf_counter_ns() - started) / 1e6

        if chosen is None:
            reason = blocking or SuppressionReason.NEGATIVE_EV
            rationale = _suppression_rationale(reason, table)
            decisions[item.id] = _suppress(
                run_id,
                item,
                table,
                reason,
                rationale,
                diagnosis.classifier_provenance,
                blocking_checks,
            )
            continue

        action = chosen.candidate.action_type
        ledger.reserve(
            item_id=item.id,
            customer_id=item.customer_id,
            action=action,
            at=chosen.candidate.earliest_executable_at,
            cost=chosen.candidate.estimated_cost,
        )
        decisions[item.id] = Decision(
            decision_id="%s:%s" % (run_id, item.id),
            item_id=item.id,
            status=DecisionStatus.CHASE,
            chosen_action=action,
            ev=chosen.breakdown.ev,
            ev_table=tuple(table),
            rationale=_chase_rationale(chosen.breakdown, action),
            provenance=diagnosis.classifier_provenance,
            policy_checks=chosen_checks,
            scheduled_for=chosen.candidate.earliest_executable_at,
            estimated_cost=chosen.candidate.estimated_cost,
        )

    ordered = [decisions[item.id] for item in items if item.id in decisions]
    return ordered, [elapsed_ms[item.id] for item in items if item.id in elapsed_ms]


def _chase_rationale(breakdown: EVBreakdown, action: ActionType) -> str:
    return (
        "%s: %.0f%% x Rs%.2f x %.0f%% margin = Rs%.2f, less Rs%.2f cost and "
        "Rs%.2f fatigue, leaves Rs%.2f expected."
        % (
            action.value,
            breakdown.p_recover * 100,
            breakdown.amount,
            breakdown.margin * 100,
            breakdown.gross_value,
            breakdown.cost,
            breakdown.fatigue_penalty,
            breakdown.ev,
        )
    )


def _suppression_rationale(
    reason: SuppressionReason, table: list[CandidateEvaluation]
) -> str:
    best = max(table, key=lambda e: e.breakdown.ev)
    if reason is SuppressionReason.BUDGET_EXHAUSTED:
        return (
            "Not worth chasing here: the budget was committed to higher-yield "
            "items first. Best action %s was worth Rs%.2f per rupee spent."
            % (best.candidate.action_type.value, best.breakdown.ev_per_rupee)
        )
    if reason is SuppressionReason.CONTACT_CAP:
        return (
            "Not worth chasing: this customer has already been contacted to the "
            "cap in the rolling window. Reaching out again costs more goodwill "
            "than the item is worth."
        )
    if reason is SuppressionReason.RETRY_CAP:
        return (
            "Not worth chasing: this item has already used its retry allowance, "
            "and each further attempt is worth less than the last."
        )
    if reason is SuppressionReason.UNRECOVERABLE_CLASS:
        return (
            "Not worth chasing: the issuer has blocked or frozen this account, "
            "so no action has any expected recovery."
        )
    return (
        "Not worth chasing: the best available action (%s) is worth Rs%.2f after "
        "cost and contact fatigue, which is below doing nothing."
        % (best.candidate.action_type.value, best.breakdown.ev)
    )
