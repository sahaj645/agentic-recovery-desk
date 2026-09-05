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
) -> tuple[list[CandidateEvaluation], CandidateEvaluation | None, float]:
    """Price every action once, marking which ones the gate would refuse.

    Returns the table, the highest-EV eligible action, and the item's best
    achievable budget density -- the density the allocator ranks it by.

    Ranking on the *best density*, not on the highest-EV action's density, is
    what stops a good cheap buy from being starved. A big-ticket timeout has two
    positive actions: a premium reroute with the higher EV but a low density
    (its cost scales with the amount), and a plain retry with lower EV but a very
    high density. If the item were ranked by the reroute's density it would sit
    near the bottom of the queue and might never be reached, even though a
    Rs2.50 retry on it is one of the best rupees the budget could spend. Ranking
    by the retry's density puts it where that cheap rupee deserves to be; Pass B
    then still spends up to the higher-EV reroute if the budget reaches that far.
    """
    table: list[CandidateEvaluation] = []
    best: CandidateEvaluation | None = None
    best_density = 0.0

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
            best_density = max(best_density, breakdown.ev_per_rupee)

    return table, best, best_density


def _no_action_reason(
    table: list[CandidateEvaluation],
) -> tuple[SuppressionReason, str]:
    """Why an item has no fundable action, told apart honestly.

    Three genuinely different situations wear the same "suppressed" badge, and
    conflating them would misreport the desk. An account can be frozen; every
    lever can be spent while the underlying item was perfectly recoverable; or
    the arithmetic simply never cleared zero. The rationale a human reads has to
    say which, because the defensible answer is different in each case.
    """
    blocked = [e for e in table if e.block_reason]
    reasons = {e.block_reason for e in blocked}

    if SuppressionReason.UNRECOVERABLE_CLASS.value in reasons:
        return (
            SuppressionReason.UNRECOVERABLE_CLASS,
            "Not worth chasing: the issuer has blocked or frozen this account, "
            "so no action has any expected recovery.",
        )

    # A positive-EV action exists but the caps refuse it: the item was
    # recoverable, we have simply run out of legitimate ways to pursue it.
    capped = [
        e
        for e in blocked
        if e.block_reason
        in (SuppressionReason.RETRY_CAP.value, SuppressionReason.CONTACT_CAP.value)
        and e.breakdown.ev > 0
    ]
    if capped:
        cap_reason = (
            SuppressionReason.RETRY_CAP
            if any(e.block_reason == SuppressionReason.RETRY_CAP.value for e in capped)
            else SuppressionReason.CONTACT_CAP
        )
        top = max(capped, key=lambda e: e.breakdown.ev)
        return (
            cap_reason,
            "Not worth chasing further: every remaining lever is spent. The only "
            "action with positive value (%s, worth Rs%.0f) is past its retry or "
            "contact cap, and pursuing it anyway is throwing good money after bad."
            % (top.candidate.action_type.value, top.breakdown.ev),
        )

    real = [
        e for e in table if e.candidate.action_type is not ActionType.DO_NOTHING
    ]
    top = max(real, key=lambda e: e.breakdown.ev) if real else table[0]
    return (
        SuppressionReason.NEGATIVE_EV,
        "Not worth chasing: the best available action (%s) is worth Rs%.2f after "
        "cost and contact fatigue, which does not clear the zero of doing nothing."
        % (top.candidate.action_type.value, top.breakdown.ev),
    )


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
        table, best, best_density = _price_item(item, diagnosis, policy, ledger)
        elapsed_ms[item.id] = (time.perf_counter_ns() - started) / 1e6

        if best is None:
            reason, rationale = _no_action_reason(table)
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
                rank_key=best_density,
            )
        )

    # -- Pass B: price the budget, then let every item buy what beats it ---
    #
    # The budget's shadow price, lambda, is the value of one more rupee of it. An
    # action is worth taking only if its expected value clears the opportunity
    # cost of the rupees it consumes -- so each item takes the action that
    # maximises (EV - lambda * cost), and takes none if even that is negative.
    #
    # This is what stops the desk both from over-buying (a premium reroute whose
    # EV barely beats a cheap retry is not worth the rupees it locks up) and from
    # under-buying (a Rs0.25 SMS is dense but leaves most of the recovery on the
    # table; net of lambda it loses to the retry that actually works). Ranking by
    # density alone does the first badly and the second catastrophically; pricing
    # the budget does both correctly, and the price it finds *is* the waterline.
    lam = _solve_lambda(scored, policy.budget)

    def rank_density(entry: _Scored) -> float:
        pick = _choose_at_lambda(_eligible_positive(entry.table), lam)
        return pick.breakdown.ev_per_rupee if pick else 0.0

    scored.sort(key=rank_density, reverse=True)

    for entry in scored:
        item, diagnosis = entry.item, entry.diagnosis
        started = time.perf_counter_ns()

        # Re-price against the live ledger: contact fatigue and the caps have
        # moved as earlier items on the same customer were committed.
        contacts = ledger.contacts_for(
            item.customer_id, entry.best.candidate.earliest_executable_at
        )
        table: list[CandidateEvaluation] = []
        affordable: list[tuple[CandidateEvaluation, tuple[PolicyCheck, ...]]] = []
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
            evaluation = CandidateEvaluation(
                candidate=candidate,
                breakdown=breakdown,
                eligible=gate.passed,
                block_reason=gate.reason.value if gate.reason else None,
            )
            table.append(evaluation)
            if gate.passed and breakdown.ev > 0:
                affordable.append((evaluation, tuple(gate.checks)))
            elif gate.reason is not None and blocking is None:
                blocking = gate.reason
                blocking_checks = tuple(gate.checks)

        # Buy the action that most beats the budget's price; none if none does.
        chosen: CandidateEvaluation | None = None
        chosen_checks: tuple[PolicyCheck, ...] = ()
        best_net = 0.0
        for evaluation, checks in affordable:
            net = evaluation.breakdown.ev - lam * evaluation.breakdown.cost
            if net > best_net + 1e-9:
                best_net = net
                chosen = evaluation
                chosen_checks = checks

        elapsed_ms[item.id] += (time.perf_counter_ns() - started) / 1e6

        if chosen is None:
            # Some action was affordable and positive but did not clear the
            # budget's price -- that is being outbid, not being unrecoverable.
            outbid = bool(affordable)
            reason = (
                SuppressionReason.BUDGET_EXHAUSTED
                if outbid
                else (blocking or SuppressionReason.NEGATIVE_EV)
            )
            rationale = _suppression_rationale(reason, table, lam)
            decisions[item.id] = _suppress(
                run_id, item, table, reason, rationale,
                diagnosis.classifier_provenance,
                () if outbid else blocking_checks,
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


def _eligible_positive(
    table: list[CandidateEvaluation],
) -> list[CandidateEvaluation]:
    return [
        e
        for e in table
        if e.eligible
        and e.candidate.action_type is not ActionType.DO_NOTHING
        and e.breakdown.ev > 0
        and e.breakdown.cost > 0
    ]


def _choose_at_lambda(
    candidates: list[CandidateEvaluation], lam: float
) -> CandidateEvaluation | None:
    """The action that most beats the budget price lambda, or none if none does."""
    chosen: CandidateEvaluation | None = None
    best_net = 0.0
    for e in candidates:
        net = e.breakdown.ev - lam * e.breakdown.cost
        if net > best_net + 1e-9:
            best_net = net
            chosen = e
    return chosen


def _solve_lambda(scored: list[_Scored], budget: float) -> float:
    """Find the budget's shadow price by bisection.

    lambda is the rupee value at which independent per-item demand exactly fits
    the budget. At lambda zero every item would take its highest-EV action; raise
    lambda and the expensive actions fall away first, then the marginal cheap
    ones, until total spend drops to the budget. The lambda that clears is the
    opportunity cost every action is charged, and the density of the last action
    still funded at that price is the waterline the trace reports.
    """
    candidate_sets = [_eligible_positive(s.table) for s in scored]
    densities = [
        e.breakdown.ev_per_rupee for cands in candidate_sets for e in cands
    ]
    if not densities:
        return 0.0

    def spend(lam: float) -> float:
        total = 0.0
        for cands in candidate_sets:
            pick = _choose_at_lambda(cands, lam)
            if pick:
                total += pick.breakdown.cost
        return total

    if spend(0.0) <= budget:
        return 0.0

    lo, hi = 0.0, max(densities)
    for _ in range(48):
        mid = (lo + hi) / 2.0
        if spend(mid) > budget:
            lo = mid
        else:
            hi = mid
    return hi


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
    reason: SuppressionReason,
    table: list[CandidateEvaluation],
    lam: float = 0.0,
) -> str:
    best = max(table, key=lambda e: e.breakdown.ev)
    if reason is SuppressionReason.BUDGET_EXHAUSTED:
        return (
            "Outbid, not unrecoverable: its best action (%s) returns Rs%.1f of "
            "expected value per rupee, but the budget filled with actions "
            "returning at least Rs%.1f per rupee. The same rupees recover more "
            "elsewhere, so chasing this one would lower total recovery."
            % (
                best.candidate.action_type.value,
                best.breakdown.ev_per_rupee,
                lam,
            )
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
