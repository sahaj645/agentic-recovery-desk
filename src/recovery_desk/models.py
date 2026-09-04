"""The typed spine of the desk.

Every entity here is immutable once constructed. Corrections are new rows, not
edits, which is what makes the audit trail trustworthy and makes replaying a
batch against a new policy version trivial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ItemType(str, Enum):
    PAYMENT_FAILURE = "payment_failure"
    SUBSCRIPTION_DUE = "subscription_due"
    INVOICE_OVERDUE = "invoice_overdue"


class FailureClass(str, Enum):
    """The five merchant-side failure classes, plus an explicit unknown.

    UNKNOWN is not a sixth class of the world; it is the classifier admitting it
    could not place the item. It carries the most pessimistic prior so that
    uncertainty costs the desk money rather than earning it optimism.
    """

    BANK_TIMEOUT = "bank_psp_timeout"
    WRONG_PIN = "wrong_pin_or_attempts_exceeded"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    NETWORK = "network_connectivity"
    ACCOUNT_BLOCKED = "account_blocked_or_frozen"
    UNKNOWN = "unknown"


class ActionType(str, Enum):
    """The closed enumeration of permitted actions.

    Anything a model proposes that does not resolve to a member of this set is
    rejected at the policy gate, logged, and counted.
    """

    DO_NOTHING = "do_nothing"
    RETRY_NOW = "retry_now"
    RETRY_SCHEDULED = "retry_scheduled"
    ALTERNATE_RAIL = "alternate_rail"
    NUDGE_SMS = "nudge_sms"
    NUDGE_WHATSAPP = "nudge_whatsapp"
    VOICE_CALL = "voice_call"


#: Actions the customer perceives. Only these accrue contact fatigue.
CONTACT_ACTIONS = frozenset(
    {ActionType.NUDGE_SMS, ActionType.NUDGE_WHATSAPP, ActionType.VOICE_CALL}
)

#: Actions that re-present the debit. Only these count against the retry cap.
RETRY_ACTIONS = frozenset(
    {ActionType.RETRY_NOW, ActionType.RETRY_SCHEDULED, ActionType.ALTERNATE_RAIL}
)


class DecisionStatus(str, Enum):
    CHASE = "chase"
    SUPPRESSED = "suppressed"


class SuppressionReason(str, Enum):
    """Why an item was not chased. Every one of these is a decision, not a failure."""

    NEGATIVE_EV = "negative_ev"
    UNRECOVERABLE_CLASS = "unrecoverable_class"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONTACT_CAP = "contact_cap"
    RETRY_CAP = "retry_cap"
    POLICY_BLOCKED = "policy_blocked"
    BASELINE_NO_ACTION = "baseline_no_action"


class Outcome(str, Enum):
    RECOVERED = "recovered"
    NOT_RECOVERED = "not_recovered"
    NOT_DISPATCHED = "not_dispatched"


# --------------------------------------------------------------------------
# Stage 1 output
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AtRiskItem:
    """One unit of revenue slipping away, normalised to a common shape.

    ``raw_gateway_context`` is deliberately unstructured: it is the only channel
    through which failure cause reaches the desk, and it is what the classifier
    has to read. Nothing here reveals the simulator's latent state.
    """

    id: str
    type: ItemType
    amount: float
    currency: str
    merchant_id: str
    customer_id: str
    occurred_at: datetime
    source: str
    raw_gateway_context: str
    prior_attempts: int = 0
    prior_contacts: int = 0


# --------------------------------------------------------------------------
# Stage 2 output
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Diagnosis:
    item_id: str
    failure_class: FailureClass
    confidence: float
    evidence: str
    recovery_prior: float
    classifier_provenance: str


# --------------------------------------------------------------------------
# Stage 3 outputs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    item_id: str
    action_type: ActionType
    estimated_cost: float
    estimated_p_recover: float
    earliest_executable_at: datetime


@dataclass(frozen=True, slots=True)
class EVBreakdown:
    """Every term of the EV kept separately, because the arithmetic is the product.

        EV = p_recover * amount * margin - cost - lambda * contact_fatigue
    """

    p_recover: float
    amount: float
    margin: float
    gross_value: float          # p_recover * amount * margin
    cost: float
    fatigue_units: float
    fatigue_penalty: float      # lambda * fatigue_units
    ev: float

    @property
    def ev_per_rupee(self) -> float:
        """Ranking key for the allocator. Zero-cost actions cannot be ranked by ratio."""
        spend = self.cost + self.fatigue_penalty
        return self.ev / spend if spend > 0 else 0.0


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    candidate: ActionCandidate
    breakdown: EVBreakdown
    eligible: bool
    block_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Decision:
    """One item, one attempt slot, one decision — with the full table that produced it."""

    decision_id: str
    item_id: str
    status: DecisionStatus
    chosen_action: ActionType | None
    ev: float
    ev_table: tuple[CandidateEvaluation, ...]
    rationale: str
    provenance: str
    policy_checks: tuple[PolicyCheck, ...]
    scheduled_for: datetime | None = None
    estimated_cost: float = 0.0
    suppression_reason: SuppressionReason | None = None
    attempt_index: int = 0

    @property
    def policy_checks_passed(self) -> bool:
        return all(c.passed for c in self.policy_checks)


# --------------------------------------------------------------------------
# Stage 4 output
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionAttempt:
    decision_id: str
    item_id: str
    idempotency_key: str
    action_type: ActionType
    dispatched_at: datetime | None
    outcome: Outcome
    amount_recovered: float
    cost_incurred: float
    dry_run: bool = True


# --------------------------------------------------------------------------
# Stage 5 output
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metrics:
    """The nine reported numbers. ``policy_violations`` non-zero is a build failure."""

    gross_recovered: float
    net_recovered: float
    recovery_rate: float
    wasted_spend: float
    chase_precision: float
    contacts_per_rupee_recovered: float
    cost_per_rupee_recovered: float
    p95_decision_latency_ms: float
    policy_violations: int
    # Supporting counts, so every ratio above can be re-derived by hand.
    items_total: int = 0
    items_chased: int = 0
    items_suppressed: int = 0
    items_recovered: int = 0
    recoverable_value: float = 0.0
    total_spend: float = 0.0
    contacts_made: int = 0


@dataclass(frozen=True, slots=True)
class BatchRun:
    id: str
    arm: str
    fixture_id: str
    policy_version: str
    budget: float
    metrics: Metrics
    started_at: datetime
    finished_at: datetime
    decisions: tuple[Decision, ...] = field(default=())
    attempts: tuple[ActionAttempt, ...] = field(default=())
