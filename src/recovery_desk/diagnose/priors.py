"""Published base rates: the desk's prior belief about what recovers.

SOURCING NOTE — read before quoting any figure here.
    The failure-mix shares below currently rest on a single industry source and
    are marked UNVERIFIED. They are used only to shape the fixture generator's
    class mix, never to make a claim on camera or in the README. The
    per-action recovery priors are the desk's *belief*, deliberately not equal
    to the simulator's outcome process — a desk that already knows the answer
    is not a desk, it is a lookup table.
"""

from __future__ import annotations

from ..models import ActionType, FailureClass

SOURCE_STATUS = "UNVERIFIED: single industry source; second source required before publication"

#: P(recover | failure class, action), before any context adjustment.
#:
#: The shape of this table is the whole argument: a wrong-PIN failure is not
#: worth retrying but is worth offering another method, an insufficient-balance
#: failure is worth retrying only on the right day, and a blocked account is
#: worth nothing at all.
RECOVERY_PRIORS: dict[FailureClass, dict[ActionType, float]] = {
    FailureClass.BANK_TIMEOUT: {
        # A PSP timeout is a transient outage: re-presenting the debit into the
        # same outage fails, but a short-delayed retry after it clears succeeds.
        # So a scheduled retry is worth far more than an immediate one, and both
        # sit just under a premium reroute that sidesteps the outage outright.
        ActionType.RETRY_NOW: 0.30,
        ActionType.RETRY_SCHEDULED: 0.52,
        ActionType.ALTERNATE_RAIL: 0.55,
        ActionType.NUDGE_SMS: 0.05,
        ActionType.NUDGE_WHATSAPP: 0.07,
        ActionType.VOICE_CALL: 0.10,
    },
    FailureClass.NETWORK: {
        ActionType.RETRY_NOW: 0.28,
        ActionType.RETRY_SCHEDULED: 0.40,
        ActionType.ALTERNATE_RAIL: 0.34,
        ActionType.NUDGE_SMS: 0.05,
        ActionType.NUDGE_WHATSAPP: 0.07,
        ActionType.VOICE_CALL: 0.09,
    },
    FailureClass.INSUFFICIENT_BALANCE: {
        ActionType.RETRY_NOW: 0.06,
        ActionType.RETRY_SCHEDULED: 0.34,
        ActionType.ALTERNATE_RAIL: 0.06,
        ActionType.NUDGE_SMS: 0.10,
        ActionType.NUDGE_WHATSAPP: 0.14,
        ActionType.VOICE_CALL: 0.18,
    },
    FailureClass.WRONG_PIN: {
        ActionType.RETRY_NOW: 0.04,
        ActionType.RETRY_SCHEDULED: 0.05,
        ActionType.ALTERNATE_RAIL: 0.08,
        ActionType.NUDGE_SMS: 0.16,
        ActionType.NUDGE_WHATSAPP: 0.22,
        ActionType.VOICE_CALL: 0.26,
    },
    FailureClass.ACCOUNT_BLOCKED: {
        ActionType.RETRY_NOW: 0.0,
        ActionType.RETRY_SCHEDULED: 0.0,
        ActionType.ALTERNATE_RAIL: 0.0,
        ActionType.NUDGE_SMS: 0.0,
        ActionType.NUDGE_WHATSAPP: 0.0,
        ActionType.VOICE_CALL: 0.0,
    },
    # Unknown is priced pessimistically on purpose: an unreadable gateway
    # response should make the desk spend less, not guess more.
    FailureClass.UNKNOWN: {
        ActionType.RETRY_NOW: 0.10,
        ActionType.RETRY_SCHEDULED: 0.12,
        ActionType.ALTERNATE_RAIL: 0.12,
        ActionType.NUDGE_SMS: 0.05,
        ActionType.NUDGE_WHATSAPP: 0.07,
        ActionType.VOICE_CALL: 0.09,
    },
}

#: Classes where no action has any expected recovery. Retrying these is the
#: purest form of wasted spend, and the desk refuses to do it.
UNRECOVERABLE_CLASSES = frozenset({FailureClass.ACCOUNT_BLOCKED})


def prior_for(failure_class: FailureClass, action: ActionType) -> float:
    if action is ActionType.DO_NOTHING:
        return 0.0
    return RECOVERY_PRIORS[failure_class][action]


def headline_prior(failure_class: FailureClass) -> float:
    """Best achievable prior across actions — the item's recoverability at a glance."""
    return max(RECOVERY_PRIORS[failure_class].values())
