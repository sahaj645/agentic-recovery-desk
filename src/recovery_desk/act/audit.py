"""The audit log: one row per decision, queryable, and shown in the demo.

Every row carries the item, the diagnosis and its evidence, every candidate
action with its priced EV, the chosen action or the suppression reason, which
component produced each input, the policy checks applied, and the outcome. It
is a first-class output of the system, not a debug artefact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from ..models import ActionAttempt, Decision, Diagnosis, DecisionStatus


@dataclass(frozen=True, slots=True)
class AuditRow:
    decision_id: str
    item_id: str
    customer_id: str
    amount: float
    failure_class: str
    evidence: str
    classifier_provenance: str
    status: str
    chosen_action: str
    ev: float
    candidates_considered: int
    suppression_reason: str | None
    policy_checks: tuple[tuple[str, bool, str], ...]
    idempotency_key: str
    dispatched_at: datetime | None
    outcome: str
    amount_recovered: float
    cost_incurred: float


def build_audit(
    decisions: Sequence[Decision],
    diagnoses: dict[str, Diagnosis],
    attempts: dict[str, ActionAttempt],
    customer_by_item: dict[str, str],
    amount_by_item: dict[str, float],
) -> list[AuditRow]:
    rows: list[AuditRow] = []
    for decision in decisions:
        diagnosis = diagnoses[decision.item_id]
        attempt = attempts.get(decision.decision_id)
        rows.append(
            AuditRow(
                decision_id=decision.decision_id,
                item_id=decision.item_id,
                customer_id=customer_by_item[decision.item_id],
                amount=amount_by_item[decision.item_id],
                failure_class=diagnosis.failure_class.value,
                evidence=diagnosis.evidence,
                classifier_provenance=diagnosis.classifier_provenance,
                status=decision.status.value,
                chosen_action=(
                    decision.chosen_action.value if decision.chosen_action else "none"
                ),
                ev=decision.ev,
                candidates_considered=len(decision.ev_table),
                suppression_reason=(
                    decision.suppression_reason.value
                    if decision.suppression_reason
                    else None
                ),
                policy_checks=tuple(
                    (c.name, c.passed, c.detail) for c in decision.policy_checks
                ),
                idempotency_key=attempt.idempotency_key if attempt else "",
                dispatched_at=attempt.dispatched_at if attempt else None,
                outcome=attempt.outcome.value if attempt else "not_dispatched",
                amount_recovered=attempt.amount_recovered if attempt else 0.0,
                cost_incurred=attempt.cost_incurred if attempt else 0.0,
            )
        )
    return rows


def dispatched_rows(rows: Sequence[AuditRow]) -> list[AuditRow]:
    return [r for r in rows if r.status == DecisionStatus.CHASE.value]
