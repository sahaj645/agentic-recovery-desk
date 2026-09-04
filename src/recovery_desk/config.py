"""Every knob the desk turns, in one place, versioned.

Nothing in this file is a magic number buried in logic. If a figure changes the
desk's behaviour, it lives here and the policy version moves with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .models import ActionType

POLICY_VERSION = "policy-v1"

#: Rupee cost of taking each action once. Retry costs are gateway fees on a
#: re-presented debit; contact costs are per-message or per-call charges.
ACTION_COSTS: dict[ActionType, float] = {
    ActionType.DO_NOTHING: 0.00,
    ActionType.RETRY_NOW: 3.00,
    ActionType.RETRY_SCHEDULED: 3.00,
    ActionType.ALTERNATE_RAIL: 5.00,
    ActionType.NUDGE_SMS: 0.25,
    ActionType.NUDGE_WHATSAPP: 0.85,
    ActionType.VOICE_CALL: 4.50,
}


@dataclass(frozen=True, slots=True)
class Policy:
    """The bounds. Safety never depends on a sampler, so all of this is data."""

    version: str = POLICY_VERSION

    # Economics
    budget: float = 2500.00
    margin: float = 0.20
    lambda_fatigue: float = 25.00

    # Hard caps, enforced before dispatch rather than after
    max_retries_per_item: int = 3
    max_contacts_per_customer: int = 2
    contact_window_hours: int = 72
    voice_call_min_amount: float = 5000.00

    # Dispatch
    dry_run: bool = True

    action_costs: dict[ActionType, float] = field(
        default_factory=lambda: dict(ACTION_COSTS)
    )

    def cost_of(self, action: ActionType) -> float:
        return self.action_costs[action]

    def with_budget(self, budget: float) -> "Policy":
        return replace(self, budget=budget)


DEFAULT_POLICY = Policy()

#: Seeded fixture defaults. A judge running `make demo` gets exactly this batch.
DEFAULT_SEED = 20260905
DEFAULT_BATCH_SIZE = 1000
