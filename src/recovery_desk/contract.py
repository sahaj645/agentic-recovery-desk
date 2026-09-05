"""The interface between the engine and any surface that displays it.

This module is the single place where engine objects become presentation data.
Nothing downstream of here may compute a decision, re-rank a queue, or invent a
number: if the UI shows it, this file produced it from a real run.

That constraint is the reason the contract is versioned and explicit rather than
"just serialise the dataclasses". A frontend that reaches into raw internals
ends up recomputing the desk badly, and then the screen and the audit log
disagree about what the desk did.

Shape, per run:

    run.json        meta, overview, queue, allocation, proof
    decisions.json  the full priced EV table for every item
    audit.json      one row per decision, with gate checks and outcome

The seven product surfaces map onto these directly:

    Overview        run.overview
    Queue           run.queue                 (filter status=chase)
    Suppressed      run.queue                 (filter status=suppressed)
    Decision detail decisions[item_id]
    Allocation      run.allocation
    Proof           run.proof
    Audit           audit.json
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from .config import Policy
from .fixtures.generator import Fixture
from .models import ActionType, DecisionStatus, Metrics
from .prove.harness import ArmResult, ablation_delta

SCHEMA_VERSION = "1.0"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _money(value: float) -> float:
    return round(value, 2)


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------


def build_overview(fixture: Fixture, arm: ArmResult, policy: Policy) -> dict[str, Any]:
    """The capital ledger: what was at stake, what was committed, what came back.

    Every figure here except ``recovered_revenue`` is the desk's own forecast,
    computed from its priors. ``recovered_revenue`` is the outcome. Keeping the
    two visibly separate is what lets an operator see when the desk was wrong.
    """
    chased = [d for d in arm.decisions if d.status is DecisionStatus.CHASE]
    suppressed = [d for d in arm.decisions if d.status is DecisionStatus.SUPPRESSED]

    # What the desk believes it could recover with an unlimited budget: the best
    # priced action on every item, whether or not it could afford to take it.
    expected_recoverable = 0.0
    for decision in arm.decisions:
        if not decision.ev_table:
            continue
        best = max(
            (e for e in decision.ev_table if e.eligible),
            key=lambda e: e.breakdown.gross_value,
            default=None,
        )
        if best:
            expected_recoverable += best.breakdown.p_recover * best.breakdown.amount

    suppressed_value = 0.0
    by_reason: dict[str, dict[str, float]] = {}
    amounts = {i.id: i.amount for i in fixture.items}
    for decision in suppressed:
        amount = amounts[decision.item_id]
        suppressed_value += amount
        reason = (
            decision.suppression_reason.value
            if decision.suppression_reason
            else "unspecified"
        )
        entry = by_reason.setdefault(reason, {"count": 0, "value": 0.0})
        entry["count"] += 1
        entry["value"] = _money(entry["value"] + amount)

    return {
        "total_at_risk": _money(fixture.total_at_risk),
        "items_at_risk": len(fixture.items),
        "recovery_budget": _money(policy.budget),
        "expected_recoverable_revenue": _money(expected_recoverable),
        "allocated_recovery_spend": _money(sum(d.estimated_cost for d in chased)),
        "expected_net_recovery": _money(sum(d.ev for d in chased)),
        "recovered_revenue": _money(arm.metrics.gross_recovered),
        "net_recovered": _money(arm.metrics.net_recovered),
        "actual_spend": _money(arm.metrics.total_spend),
        "wasted_spend": _money(arm.metrics.wasted_spend),
        "items_chased": len(chased),
        "suppressed": {
            "count": len(suppressed),
            "value": _money(suppressed_value),
            "share_of_pool": round(len(suppressed) / max(len(arm.decisions), 1), 4),
            "by_reason": by_reason,
        },
    }


# --------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------


def build_queue(fixture: Fixture, arm: ArmResult) -> list[dict[str, Any]]:
    """The ranked list. Order is the order the desk actually spent in."""
    items = {i.id: i for i in fixture.items}
    outcomes = {a.decision_id: a for a in arm.attempts}
    rows: list[dict[str, Any]] = []

    for decision in arm.decisions:
        item = items[decision.item_id]
        diagnosis = arm.diagnoses[decision.item_id]
        attempt = outcomes.get(decision.decision_id)

        chosen = None
        for evaluation in decision.ev_table:
            if evaluation.candidate.action_type is decision.chosen_action:
                chosen = evaluation
                break

        rows.append(
            {
                "item_id": item.id,
                "customer_id": item.customer_id,
                "amount": _money(item.amount),
                "currency": item.currency,
                "occurred_at": _iso(item.occurred_at),
                "source": item.source,
                "failure_class": diagnosis.failure_class.value,
                "confidence": round(diagnosis.confidence, 3),
                "recovery_probability": (
                    round(chosen.breakdown.p_recover, 4) if chosen else 0.0
                ),
                "recommended_action": (
                    decision.chosen_action.value
                    if decision.chosen_action
                    else ActionType.DO_NOTHING.value
                ),
                "action_cost": _money(decision.estimated_cost),
                "expected_value": _money(decision.ev),
                "ev_per_rupee": (
                    round(chosen.breakdown.ev_per_rupee, 3) if chosen else 0.0
                ),
                "contact_fatigue": (
                    round(chosen.breakdown.fatigue_units, 2) if chosen else 0.0
                ),
                "fatigue_penalty": (
                    _money(chosen.breakdown.fatigue_penalty) if chosen else 0.0
                ),
                "status": decision.status.value,
                "suppression_reason": (
                    decision.suppression_reason.value
                    if decision.suppression_reason
                    else None
                ),
                "rationale": decision.rationale,
                "scheduled_for": _iso(decision.scheduled_for),
                "outcome": attempt.outcome.value if attempt else "not_dispatched",
                "amount_recovered": (
                    _money(attempt.amount_recovered) if attempt else 0.0
                ),
                "decision_id": decision.decision_id,
            }
        )

    # Chased first, best value per rupee first within that, then the suppressed
    # pool by size -- an operator reviewing "what did we skip" wants the big
    # ones at the top.
    rows.sort(
        key=lambda r: (
            0 if r["status"] == "chase" else 1,
            -r["ev_per_rupee"] if r["status"] == "chase" else -r["amount"],
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


# --------------------------------------------------------------------------
# Decision detail
# --------------------------------------------------------------------------


def build_decisions(fixture: Fixture, arm: ArmResult) -> dict[str, Any]:
    """Every priced action for every item -- the signature surface of the desk."""
    items = {i.id: i for i in fixture.items}
    outcomes = {a.decision_id: a for a in arm.attempts}
    detail: dict[str, Any] = {}

    for decision in arm.decisions:
        item = items[decision.item_id]
        diagnosis = arm.diagnoses[decision.item_id]
        attempt = outcomes.get(decision.decision_id)

        detail[decision.item_id] = {
            "decision_id": decision.decision_id,
            "payment": {
                "item_id": item.id,
                "type": item.type.value,
                "source": item.source,
                "customer_id": item.customer_id,
                "merchant_id": item.merchant_id,
                "occurred_at": _iso(item.occurred_at),
                "prior_attempts": item.prior_attempts,
                "prior_contacts": item.prior_contacts,
            },
            "amount": _money(item.amount),
            "currency": item.currency,
            "failure": {
                "class": diagnosis.failure_class.value,
                "confidence": round(diagnosis.confidence, 3),
                "evidence": diagnosis.evidence,
                "raw_gateway_context": item.raw_gateway_context,
                "recovery_prior": round(diagnosis.recovery_prior, 4),
                "provenance": diagnosis.classifier_provenance,
            },
            "candidate_actions": [
                {
                    "action": e.candidate.action_type.value,
                    "probability": round(e.breakdown.p_recover, 4),
                    "amount": _money(e.breakdown.amount),
                    "margin": e.breakdown.margin,
                    "gross_value": _money(e.breakdown.gross_value),
                    "cost": _money(e.breakdown.cost),
                    "fatigue_units": round(e.breakdown.fatigue_units, 2),
                    "fatigue_penalty": _money(e.breakdown.fatigue_penalty),
                    "expected_value": _money(e.breakdown.ev),
                    "ev_per_rupee": round(e.breakdown.ev_per_rupee, 3),
                    "eligible": e.eligible,
                    "block_reason": e.block_reason,
                    "earliest_executable_at": _iso(
                        e.candidate.earliest_executable_at
                    ),
                    "selected": e.candidate.action_type is decision.chosen_action
                    and decision.status is DecisionStatus.CHASE,
                }
                for e in decision.ev_table
            ],
            "selected_action": (
                decision.chosen_action.value if decision.chosen_action else None
            ),
            "status": decision.status.value,
            "suppression_reason": (
                decision.suppression_reason.value
                if decision.suppression_reason
                else None
            ),
            "rationale": decision.rationale,
            "provenance": decision.provenance,
            "scheduled_for": _iso(decision.scheduled_for),
            "policy_checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in decision.policy_checks
            ],
            "outcome": {
                "state": attempt.outcome.value if attempt else "not_dispatched",
                "idempotency_key": attempt.idempotency_key if attempt else "",
                "dispatched_at": _iso(attempt.dispatched_at) if attempt else None,
                "amount_recovered": (
                    _money(attempt.amount_recovered) if attempt else 0.0
                ),
                "cost_incurred": _money(attempt.cost_incurred) if attempt else 0.0,
                "dry_run": attempt.dry_run if attempt else True,
            },
        }
    return detail


# --------------------------------------------------------------------------
# Allocation
# --------------------------------------------------------------------------


def build_allocation(
    fixture: Fixture, arm: ArmResult, policy: Policy
) -> dict[str, Any]:
    """How the finite budget was split, and exactly where it ran out."""
    items = {i.id: i for i in fixture.items}
    chased = [d for d in arm.decisions if d.status is DecisionStatus.CHASE]

    by_action: dict[str, dict[str, Any]] = {}
    for decision in chased:
        key = decision.chosen_action.value
        entry = by_action.setdefault(
            key, {"action": key, "count": 0, "spend": 0.0, "expected_value": 0.0}
        )
        entry["count"] += 1
        entry["spend"] = _money(entry["spend"] + decision.estimated_cost)
        entry["expected_value"] = _money(entry["expected_value"] + decision.ev)

    by_class: dict[str, dict[str, Any]] = {}
    for decision in arm.decisions:
        diagnosis = arm.diagnoses[decision.item_id]
        key = diagnosis.failure_class.value
        entry = by_class.setdefault(
            key,
            {
                "failure_class": key,
                "pool_count": 0,
                "pool_value": 0.0,
                "chased": 0,
                "suppressed": 0,
                "spend": 0.0,
            },
        )
        entry["pool_count"] += 1
        entry["pool_value"] = _money(
            entry["pool_value"] + items[decision.item_id].amount
        )
        if decision.status is DecisionStatus.CHASE:
            entry["chased"] += 1
            entry["spend"] = _money(entry["spend"] + decision.estimated_cost)
        else:
            entry["suppressed"] += 1

    # The greedy frontier, in the order the desk committed. The point at which
    # this curve stops is the budget waterline the queue draws.
    ordered = sorted(
        chased,
        key=lambda d: (d.ev / d.estimated_cost) if d.estimated_cost else 0.0,
        reverse=True,
    )
    curve: list[dict[str, Any]] = []
    cumulative_spend = 0.0
    cumulative_ev = 0.0
    for position, decision in enumerate(ordered, start=1):
        cumulative_spend += decision.estimated_cost
        cumulative_ev += decision.ev
        # One point per item is more resolution than any chart needs; sample.
        if position % 5 == 0 or position == len(ordered):
            curve.append(
                {
                    "position": position,
                    "cumulative_spend": _money(cumulative_spend),
                    "cumulative_expected_value": _money(cumulative_ev),
                    "marginal_ev_per_rupee": round(
                        (decision.ev / decision.estimated_cost)
                        if decision.estimated_cost
                        else 0.0,
                        3,
                    ),
                }
            )

    return {
        "budget": _money(policy.budget),
        "committed": _money(sum(d.estimated_cost for d in chased)),
        "remaining": _money(policy.budget - sum(d.estimated_cost for d in chased)),
        "items_funded": len(chased),
        "items_unfunded": sum(
            1
            for d in arm.decisions
            if d.suppression_reason
            and d.suppression_reason.value == "budget_exhausted"
        ),
        "by_action": sorted(
            by_action.values(), key=lambda e: e["spend"], reverse=True
        ),
        "by_failure_class": sorted(
            by_class.values(), key=lambda e: e["pool_value"], reverse=True
        ),
        "marginal_curve": curve,
    }


# --------------------------------------------------------------------------
# Proof
# --------------------------------------------------------------------------


def metrics_dict(metrics: Metrics) -> dict[str, Any]:
    return {
        "gross_recovered": _money(metrics.gross_recovered),
        "net_recovered": _money(metrics.net_recovered),
        "recovery_rate": round(metrics.recovery_rate, 4),
        "wasted_spend": _money(metrics.wasted_spend),
        "chase_precision": round(metrics.chase_precision, 4),
        "contacts_per_rupee_recovered": round(
            metrics.contacts_per_rupee_recovered, 6
        ),
        "cost_per_rupee_recovered": round(metrics.cost_per_rupee_recovered, 6),
        "p95_decision_latency_ms": round(metrics.p95_decision_latency_ms, 3),
        "policy_violations": metrics.policy_violations,
        "items_total": metrics.items_total,
        "items_chased": metrics.items_chased,
        "items_suppressed": metrics.items_suppressed,
        "items_recovered": metrics.items_recovered,
        "recoverable_value": _money(metrics.recoverable_value),
        "total_spend": _money(metrics.total_spend),
        "contacts_made": metrics.contacts_made,
    }


def build_proof(results: Sequence[ArmResult]) -> dict[str, Any]:
    """The arm comparison, plus what the model was actually worth."""
    arms = [
        {
            "arm_id": r.arm_id,
            "name": r.name,
            "description": r.description,
            "classifier": r.classifier_provenance,
            "metrics": metrics_dict(r.metrics),
        }
        for r in results
    ]

    by_id = {r.arm_id: r for r in results}
    primary = by_id.get("B3") or by_id.get("B3*")
    deltas: dict[str, Any] = {}
    if primary is not None:
        for reference in ("B1", "B2"):
            other = by_id.get(reference)
            if other is None:
                continue
            deltas[reference] = {
                "net_recovered": _money(
                    primary.metrics.net_recovered - other.metrics.net_recovered
                ),
                "net_recovered_pct": (
                    round(
                        100
                        * (primary.metrics.net_recovered - other.metrics.net_recovered)
                        / other.metrics.net_recovered,
                        1,
                    )
                    if other.metrics.net_recovered
                    else None
                ),
                "wasted_spend": _money(
                    primary.metrics.wasted_spend - other.metrics.wasted_spend
                ),
                "chase_precision": round(
                    primary.metrics.chase_precision - other.metrics.chase_precision, 4
                ),
            }

    ablation = ablation_delta(list(results))
    return {
        "arms": arms,
        "primary_arm": primary.arm_id if primary else None,
        "deltas": deltas,
        "ablation": (
            {k: _money(v) if k != "chase_precision" else round(v, 4)
             for k, v in ablation.items()}
            if ablation
            else None
        ),
        "ablation_note": (
            "B3 minus B3*: the same desk on the model classifier versus the "
            "deterministic one. Without a model arm this is null, and no claim "
            "about the model is made."
        ),
    }


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def build_audit_rows(arm: ArmResult) -> list[dict[str, Any]]:
    return [
        {
            "decision_id": row.decision_id,
            "item_id": row.item_id,
            "customer_id": row.customer_id,
            "amount": _money(row.amount),
            "failure_class": row.failure_class,
            "evidence": row.evidence,
            "classifier_provenance": row.classifier_provenance,
            "status": row.status,
            "chosen_action": row.chosen_action,
            "expected_value": _money(row.ev),
            "candidates_considered": row.candidates_considered,
            "suppression_reason": row.suppression_reason,
            "policy_checks": [
                {"name": name, "passed": passed, "detail": detail}
                for name, passed, detail in row.policy_checks
            ],
            "idempotency_key": row.idempotency_key,
            "dispatched_at": _iso(row.dispatched_at),
            "outcome": row.outcome,
            "amount_recovered": _money(row.amount_recovered),
            "cost_incurred": _money(row.cost_incurred),
        }
        for row in arm.audit
    ]


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_run(
    fixture: Fixture,
    results: Sequence[ArmResult],
    policy: Policy,
    primary_arm_id: str | None = None,
) -> dict[str, Any]:
    """The document every surface reads. One run, one source of truth."""
    by_id = {r.arm_id: r for r in results}
    arm = by_id.get(primary_arm_id or "") or by_id.get("B3") or by_id["B3*"]

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": arm.run.id,
        "generated_at": _iso(arm.run.finished_at),
        "fixture": {
            "id": fixture.id,
            "seed": fixture.seed,
            "size": fixture.size,
            "total_at_risk": _money(fixture.total_at_risk),
        },
        "policy": {
            "version": policy.version,
            "budget": _money(policy.budget),
            "margin": policy.margin,
            "lambda_fatigue": policy.lambda_fatigue,
            "max_retries_per_item": policy.max_retries_per_item,
            "max_contacts_per_customer": policy.max_contacts_per_customer,
            "contact_window_hours": policy.contact_window_hours,
            "dry_run": policy.dry_run,
            "action_costs": {a.value: c for a, c in policy.action_costs.items()},
        },
        "arm": {
            "arm_id": arm.arm_id,
            "name": arm.name,
            "classifier": arm.classifier_provenance,
        },
        "overview": build_overview(fixture, arm, policy),
        "queue": build_queue(fixture, arm),
        "allocation": build_allocation(fixture, arm, policy),
        "proof": build_proof(results),
    }


# --------------------------------------------------------------------------
# Workspace (single-arm view, for the allocation-workspace UI)
# --------------------------------------------------------------------------


def build_workspace(fixture: Fixture, arm: ArmResult, policy: Policy) -> dict[str, Any]:
    """The document the allocation-workspace frontend reads.

    Narrower than ``build_run``: one arm, no baseline comparison. It exists
    because the workspace UI shows the funnel, the queue and the decision
    detail for the desk itself, not the B0-B3 arm comparison -- that is the
    Proof surface's job, and it reads ``build_run`` instead.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": arm.run.id,
        "generated_at": _iso(arm.run.finished_at),
        "fixture": {
            "id": fixture.id,
            "seed": fixture.seed,
            "size": fixture.size,
            "total_at_risk": _money(fixture.total_at_risk),
        },
        "policy": {
            "version": policy.version,
            "budget": _money(policy.budget),
            "margin": policy.margin,
            "lambda_fatigue": policy.lambda_fatigue,
            "max_retries_per_item": policy.max_retries_per_item,
            "max_contacts_per_customer": policy.max_contacts_per_customer,
            "contact_window_hours": policy.contact_window_hours,
            "dry_run": policy.dry_run,
        },
        "arm": {
            "arm_id": arm.arm_id,
            "name": arm.name,
            "classifier": arm.classifier_provenance,
        },
        "overview": build_overview(fixture, arm, policy),
        "queue": build_queue(fixture, arm),
    }
