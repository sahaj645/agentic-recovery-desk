"""The policy gate. Nothing reaches dispatch without passing through here.

Every check in this file is deterministic and every one of them is data-driven
from ``Policy``. Safety must never depend on a sampler, so no model output is
consulted at this boundary -- a model can only ever *propose* an action, and
the proposal is either a member of the closed enumeration or it is rejected,
logged and counted.
"""

from __future__ import annotations

from datetime import datetime

from ..act.ledger import Ledger
from ..config import Policy
from ..diagnose import priors
from ..models import (
    CONTACT_ACTIONS,
    RETRY_ACTIONS,
    ActionType,
    AtRiskItem,
    Diagnosis,
    PolicyCheck,
    SuppressionReason,
)


class GateResult:
    __slots__ = ("checks", "reason")

    def __init__(self, checks: list[PolicyCheck], reason: SuppressionReason | None):
        self.checks = checks
        self.reason = reason

    @property
    def passed(self) -> bool:
        return self.reason is None


def evaluate_gate(
    item: AtRiskItem,
    diagnosis: Diagnosis,
    action: ActionType,
    cost: float,
    executed_at: datetime,
    policy: Policy,
    ledger: Ledger,
) -> GateResult:
    """Run every bound against one proposed action, in order of severity."""
    checks: list[PolicyCheck] = []
    reason: SuppressionReason | None = None

    def check(name: str, passed: bool, detail: str, fail_reason: SuppressionReason):
        nonlocal reason
        checks.append(PolicyCheck(name=name, passed=passed, detail=detail))
        if not passed and reason is None:
            reason = fail_reason

    # 1. Closed action enumeration. Anything not in the enum never got this far,
    #    but the check is explicit because it is the bound we claim in the README.
    check(
        "action_in_closed_set",
        isinstance(action, ActionType),
        "%s is a permitted action" % action.value,
        SuppressionReason.POLICY_BLOCKED,
    )

    # 2. Refuse to chase an account the issuer has blocked or frozen. Stated as
    #    an explicit refusal in the README, not an optimisation.
    blocked_class = diagnosis.failure_class in priors.UNRECOVERABLE_CLASSES
    check(
        "recoverable_class",
        not (blocked_class and action is not ActionType.DO_NOTHING),
        "blocked or frozen account: no action permitted"
        if blocked_class
        else "class %s is chaseable" % diagnosis.failure_class.value,
        SuppressionReason.UNRECOVERABLE_CLASS,
    )

    # 3. Retry cap per item, enforced before dispatch.
    if action in RETRY_ACTIONS:
        used = ledger.retries_for(item.id) + item.prior_attempts
        check(
            "retry_cap",
            used < policy.max_retries_per_item,
            "%d of %d retries used" % (used, policy.max_retries_per_item),
            SuppressionReason.RETRY_CAP,
        )

    # 4. Contact cap per customer, across all channels, in a rolling window.
    if action in CONTACT_ACTIONS:
        used = ledger.contacts_for(item.customer_id, executed_at) + item.prior_contacts
        check(
            "contact_cap",
            used < policy.max_contacts_per_customer,
            "%d of %d contacts in %dh window"
            % (used, policy.max_contacts_per_customer, policy.contact_window_hours),
            SuppressionReason.CONTACT_CAP,
        )

    # 5. A voice call is only proportionate above a value threshold.
    if action is ActionType.VOICE_CALL:
        check(
            "voice_call_threshold",
            item.amount >= policy.voice_call_min_amount,
            "amount %.2f against floor %.2f" % (item.amount, policy.voice_call_min_amount),
            SuppressionReason.POLICY_BLOCKED,
        )

    # 6. Spend ceiling, checked before this dispatch rather than after it.
    check(
        "budget_ceiling",
        ledger.can_afford(cost),
        "cost %.2f against %.2f remaining" % (cost, ledger.remaining_budget),
        SuppressionReason.BUDGET_EXHAUSTED,
    )

    # 7. Kill switch.
    check(
        "not_halted",
        not ledger.halted,
        "dispatch halted" if ledger.halted else "dispatch active",
        SuppressionReason.POLICY_BLOCKED,
    )

    return GateResult(checks, reason)


def coerce_proposed_action(raw: str) -> ActionType | None:
    """Resolve a proposed action name to the closed set, or reject it.

    This is the single place a model-originated action name can enter the
    system. It returns None rather than raising, so the caller counts the
    rejection instead of the batch failing.
    """
    try:
        return ActionType(raw)
    except ValueError:
        return None
