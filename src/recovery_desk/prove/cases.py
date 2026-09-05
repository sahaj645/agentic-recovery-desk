"""Representative cases: five concrete items that make the comparison legible.

A table of aggregate metrics tells you the desk is better. It does not tell
you *why*, and an aggregate is easy to be skeptical of. This module finds real
items from one real evaluation run -- never invented, never staged -- where the
mechanism is visible in a single decision: a baseline burning money on a wasted
retry, a rules-only miss the salary-cycle rule catches, the desk turning down a
bad bet, choosing a smaller bet over a bigger one, and standing down entirely.

Every field in a returned case comes from a real ``Decision`` or
``ActionAttempt`` object already computed by the harness. The one exception is
labelling a case with its *ground-truth* failure class for narrative clarity --
that is safe here because it is used only to describe the evidence after the
fact, never to feed a decision. The circularity firewall is about what the desk
can see when it decides, not about what a human evidence report can show
afterward.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..fixtures.generator import Fixture
from ..models import DecisionStatus, Outcome, SuppressionReason
from .harness import ArmResult


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    title: str
    arm_id: str
    item_id: str
    amount: float
    narrative: str
    evidence: dict[str, Any]


def _arms_by_id(results: list[ArmResult]) -> dict[str, ArmResult]:
    return {r.arm_id: r for r in results}


def _b1_wasted_spend(fixture: Fixture, b1: ArmResult | None) -> Case | None:
    """A blanket retry spent real money on an item that could never recover.

    B1 has no failure classification, so it cannot tell a transient timeout from
    a frozen account. The clearest waste is a retry fired at an account the
    issuer has already blocked -- structurally unrecoverable, priced at zero by
    the desk, retried anyway by a policy that never looked.
    """
    if b1 is None:
        return None
    truth = fixture.ground_truth
    spend_by_item: dict[str, float] = {}
    attempts_by_item: dict[str, int] = {}
    for attempt in b1.attempts:
        if attempt.cost_incurred > 0:
            spend_by_item[attempt.item_id] = (
                spend_by_item.get(attempt.item_id, 0.0) + attempt.cost_incurred
            )
            attempts_by_item[attempt.item_id] = attempts_by_item.get(attempt.item_id, 0) + 1

    candidates = [
        (item_id, cost)
        for item_id, cost in spend_by_item.items()
        if truth[item_id].true_class.value == "account_blocked_or_frozen"
    ]
    if not candidates:
        # Fall back to any item retried repeatedly with nothing ever recovered.
        recovered = {a.item_id for a in b1.attempts if a.outcome is Outcome.RECOVERED}
        candidates = [
            (item_id, cost)
            for item_id, cost in spend_by_item.items()
            if item_id not in recovered and attempts_by_item.get(item_id, 0) >= 2
        ]
    if not candidates:
        return None

    item_id, cost = max(candidates, key=lambda c: c[1])
    amount = next(i.amount for i in fixture.items if i.id == item_id)
    attempts = attempts_by_item[item_id]
    return Case(
        id="b1-wasted-spend",
        title="Blanket retry spends on an account that was never coming back",
        arm_id="B1",
        item_id=item_id,
        amount=amount,
        narrative=(
            "This item's account has been blocked or frozen by the issuer -- no "
            "action has any chance of recovering it. Blanket retry does not "
            "classify failures, so it fired %d retry attempt(s) at Rs%.2f each "
            "before its own retry cap stopped it, recovering nothing. Recovery "
            "Desk prices this class at zero and suppresses it on the first look."
            % (attempts, cost / attempts)
        ),
        evidence={
            "true_failure_class": truth[item_id].true_class.value,
            "attempts_fired": attempts,
            "spend": round(cost, 2),
            "amount_recovered": 0.0,
        },
    )


def _b2_missed_opportunity(
    fixture: Fixture, b2: ArmResult | None, desk: ArmResult | None
) -> Case | None:
    """Where the salary-cycle rule earns its keep against the honest bar.

    B2 already knows to wait on an insufficient-balance failure -- it is the
    strongest static policy precisely because it does that much. What it does
    not have is the calendar: it waits a fixed 24 hours regardless of when the
    salary credit actually lands. The desk schedules against the credit itself.
    This finds a real item where that difference changed the outcome.
    """
    if b2 is None or desk is None:
        return None
    b2_outcome = {a.item_id: a for a in b2.attempts}
    desk_outcome = {a.item_id: a for a in desk.attempts}
    truth = fixture.ground_truth

    candidates = []
    for item in fixture.items:
        if truth[item.id].true_class.value != "insufficient_balance":
            continue
        b2a = b2_outcome.get(item.id)
        da = desk_outcome.get(item.id)
        if (
            b2a is not None
            and da is not None
            and b2a.outcome is not Outcome.RECOVERED
            and da.outcome is Outcome.RECOVERED
        ):
            candidates.append(item)
    if not candidates:
        return None

    item = max(candidates, key=lambda i: i.amount)
    b2a = b2_outcome[item.id]
    da = desk_outcome[item.id]
    return Case(
        id="b2-missed-opportunity",
        title="The rules-only bar waits a fixed day; the desk waits for payday",
        arm_id="B2",
        item_id=item.id,
        amount=item.amount,
        narrative=(
            "A balance failure is a calendar problem, not a backoff problem. "
            "Rules-only retries this item on a fixed 24-hour clock and misses -- "
            "the salary credit had not landed yet. Recovery Desk schedules the "
            "same retry against the salary-credit calendar instead and recovers "
            "Rs%.2f. Same item, same action type, different clock." % item.amount
        ),
        evidence={
            "true_failure_class": "insufficient_balance",
            "b2_scheduled_for": b2a.dispatched_at.isoformat() if b2a.dispatched_at else None,
            "b2_outcome": b2a.outcome.value,
            "desk_scheduled_for": da.dispatched_at.isoformat() if da.dispatched_at else None,
            "desk_outcome": da.outcome.value,
            "desk_amount_recovered": round(da.amount_recovered, 2),
        },
    )


def _desk_suppressed_poor_opportunity(desk: ArmResult | None) -> Case | None:
    """The desk turns down a bad bet and shows the arithmetic that decided it.

    Restricted to real actions (excludes do-nothing, which always prices at
    exactly zero and would otherwise win every comparison by definition) so the
    "best" figure quoted is the best *real* action considered, not the floor.
    """
    if desk is None:
        return None
    negative = [
        d
        for d in desk.decisions
        if d.status is DecisionStatus.SUPPRESSED
        and d.suppression_reason is SuppressionReason.NEGATIVE_EV
        and d.ev_table
    ]
    if not negative:
        return None

    def best_real(d):
        real = [e for e in d.ev_table if e.candidate.action_type.value != "do_nothing"]
        return max((e.breakdown.ev for e in real), default=-1e9)

    decision = max(negative, key=best_real)
    real = [
        e for e in decision.ev_table if e.candidate.action_type.value != "do_nothing"
    ]
    best = max(real, key=lambda e: e.breakdown.ev)
    amount = best.breakdown.amount
    return Case(
        id="desk-suppressed-negative-ev",
        title="The desk turns down every available action on a real payment",
        arm_id="B3*",
        item_id=decision.item_id,
        amount=amount,
        narrative=(
            "Every candidate action on this Rs%.2f payment was priced -- retries, "
            "reroute, every contact channel -- and the best real one (%s) still "
            "nets only Rs%.2f once cost and contact fatigue are subtracted. That "
            "does not clear the zero of doing nothing, so the desk suppresses it "
            "and records why, rather than spending anyway."
            % (amount, best.candidate.action_type.value, best.breakdown.ev)
        ),
        evidence={
            "candidates_priced": len(decision.ev_table),
            "best_real_action": best.candidate.action_type.value,
            "best_real_action_ev": round(best.breakdown.ev, 2),
            "rationale": decision.rationale,
        },
    )


def _desk_smaller_but_better(desk: ArmResult | None) -> Case | None:
    """A smaller payment funded while a larger one is outbid -- amount is not the story."""
    if desk is None:
        return None
    chased = {
        d.item_id: d for d in desk.decisions if d.status is DecisionStatus.CHASE
    }
    outbid = [
        d
        for d in desk.decisions
        if d.status is DecisionStatus.SUPPRESSED
        and d.suppression_reason is SuppressionReason.BUDGET_EXHAUSTED
    ]
    if not chased or not outbid:
        return None

    amount_of = {}
    for d in list(chased.values()) + outbid:
        best = max((e.breakdown.amount for e in d.ev_table), default=0.0)
        amount_of[d.item_id] = best

    best_pair = None
    best_ratio = 1.0
    for big in outbid:
        big_amount = amount_of.get(big.item_id, 0.0)
        if big_amount <= 0:
            continue
        for small_id, small in chased.items():
            small_amount = amount_of.get(small_id, 0.0)
            if small_amount <= 0 or small_amount >= big_amount:
                continue
            ratio = big_amount / small_amount
            if ratio > best_ratio:
                best_ratio = ratio
                best_pair = (small, big, small_amount, big_amount)

    if best_pair is None:
        return None
    small, big, small_amount, big_amount = best_pair
    return Case(
        id="desk-smaller-but-better",
        title="A smaller payment wins the budget over a larger one",
        arm_id="B3*",
        item_id=small.item_id,
        amount=small_amount,
        narrative=(
            "Rs%.2f is funded (EV Rs%.2f) while a Rs%.2f payment sits outbid at "
            "the budget's waterline. The larger item is genuinely recoverable -- "
            "it simply returns less expected value per rupee of budget than the "
            "smaller one does, and the same rupees recover more used here."
            % (small_amount, small.ev, big_amount)
        ),
        evidence={
            "funded_item": small.item_id,
            "funded_amount": round(small_amount, 2),
            "funded_ev": round(small.ev, 2),
            "outbid_item": big.item_id,
            "outbid_amount": round(big_amount, 2),
            "amount_ratio": round(best_ratio, 2),
        },
    )


def _desk_do_nothing(desk: ArmResult | None) -> Case | None:
    """The clearest do-nothing: a hard policy refusal, not a close call.

    This is the "explicit refusal" the design calls for, not merely a case where
    the arithmetic happened to net negative. The blended prior can still price a
    retry on a blocked account above zero on paper (a classifier with imperfect
    confidence borrows a little optimism from the unknown-class prior) -- and the
    evidence deliberately surfaces that number, because the point is that the
    policy gate refuses the action anyway. The rule does not consult the EV.
    """
    if desk is None:
        return None
    unrecoverable = [
        d
        for d in desk.decisions
        if d.status is DecisionStatus.SUPPRESSED
        and d.suppression_reason is SuppressionReason.UNRECOVERABLE_CLASS
        and d.ev_table
    ]
    if not unrecoverable:
        return None
    decision = max(
        unrecoverable,
        key=lambda d: max((e.breakdown.amount for e in d.ev_table), default=0.0),
    )
    real = [
        e for e in decision.ev_table if e.candidate.action_type.value != "do_nothing"
    ]
    amount = max((e.breakdown.amount for e in real), default=0.0)
    blocked = [e for e in real if e.block_reason == "unrecoverable_class"]
    priced_above_zero = [e for e in blocked if e.breakdown.ev > 0]
    return Case(
        id="desk-do-nothing",
        title="Doing nothing is the economically optimal decision",
        arm_id="B3*",
        item_id=decision.item_id,
        amount=amount,
        narrative=(
            "The issuer has blocked or frozen this account. The policy gate "
            "refuses every one of the %d real actions on it outright -- this is "
            "an explicit rule, not an EV calculation: %d of them still price "
            "above zero on paper (a low-confidence diagnosis borrows a little "
            "optimism from the unknown-class prior), and the gate blocks them "
            "anyway, because a frozen account is not a probability question."
            % (len(real), len(priced_above_zero))
        ),
        evidence={
            "candidates_priced": len(decision.ev_table),
            "actions_refused_by_policy_gate": len(blocked),
            "of_which_priced_above_zero_anyway": len(priced_above_zero),
            "rationale": decision.rationale,
        },
    )


def find_cases(
    fixture: Fixture,
    results: list[ArmResult],
    scenario_fixture: Fixture | None = None,
    scenario_result: ArmResult | None = None,
) -> list[Case]:
    """The five cases the design asks the evidence to show, drawn from real runs.

    Four are found directly in the evaluation fixture -- the same 1,000-item,
    seeded, unbiased batch the B0-B3 comparison itself runs on. The fifth,
    "smaller but better," needs the budget to actually bind against more than
    one competing item at once. At the evaluation's standard Rs2,500 budget an
    unbiased 1,000-item draw does not demand enough of the premium tail to
    outbid anything -- a genuine finding, not a gap -- so that case only shows up
    at all when a caller also supplies the constructed scarcity scenario
    (``fixtures/scenario.py``) already used for the allocation-workspace demo.
    Every such case is labelled with which fixture it came from; nothing here
    blends the two or hides the source.
    """
    by_id = _arms_by_id(results)
    b1 = by_id.get("B1")
    b2 = by_id.get("B2")
    desk = by_id.get("B3") or by_id.get("B3*")

    cases: list[Case] = []
    for finder in (
        lambda: _b1_wasted_spend(fixture, b1),
        lambda: _b2_missed_opportunity(fixture, b2, desk),
        lambda: _desk_suppressed_poor_opportunity(desk),
        lambda: _desk_smaller_but_better(desk),
        lambda: _desk_do_nothing(desk),
    ):
        case = finder()
        if case is not None:
            cases.append(case)

    have_smaller_better = any(c.id == "desk-smaller-but-better" for c in cases)
    if not have_smaller_better and scenario_result is not None:
        case = _desk_smaller_but_better(scenario_result)
        if case is not None:
            cases.append(
                Case(
                    id=case.id,
                    title=case.title,
                    arm_id=case.arm_id,
                    item_id=case.item_id,
                    amount=case.amount,
                    narrative=(
                        case.narrative
                        + " (From the constructed scarcity scenario, not the "
                        "1,000-item evaluation batch: at the evaluation's "
                        "standard budget, demand does not exceed the budget "
                        "hard enough for this to occur -- see docs/failures.md.)"
                    ),
                    evidence={**case.evidence, "source_fixture": "scenario"},
                )
            )

    return cases


def cases_to_dicts(cases: list[Case]) -> list[dict[str, Any]]:
    return [
        {
            "id": c.id,
            "title": c.title,
            "arm_id": c.arm_id,
            "item_id": c.item_id,
            "amount": round(c.amount, 2),
            "narrative": c.narrative,
            "evidence": c.evidence,
        }
        for c in cases
    ]
