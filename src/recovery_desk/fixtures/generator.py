"""Seeded fixture generation.

The generator's job is to produce a pool that is hard in the way the real world
is hard: the failure cause reaches the desk only as messy gateway text, some of
which no keyword matcher will ever place, and recoverability depends on facts
the desk cannot see.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import DEFAULT_BATCH_SIZE, DEFAULT_SEED
from ..models import AtRiskItem, FailureClass, ItemType
from .world import GroundTruth, Latent, World, build_ground_truth, draw_latent

BATCH_START = datetime(2026, 8, 1, 0, 0, 0)
BATCH_DAYS = 30

# Share of the pool by true failure class. This belongs to the world, not to
# the desk: the desk holds a *belief* about recovery in diagnose/priors.py and
# is never given this table. Shares follow published merchant-side
# distributions, which are still single-sourced -- see priors.SOURCE_STATUS.
POOL_MIX: dict[FailureClass, float] = {
    FailureClass.BANK_TIMEOUT: 0.40,
    FailureClass.WRONG_PIN: 0.25,
    FailureClass.INSUFFICIENT_BALANCE: 0.20,
    FailureClass.NETWORK: 0.10,
    FailureClass.ACCOUNT_BLOCKED: 0.05,
}

# Gateway text the desk has to read. "easy" strings carry a keyword the
# deterministic matcher knows; "hard" strings are real-world formats that carry
# the same meaning with none of the vocabulary. The hard set is where a model
# can earn its place — and where it can be measured doing so.
TEXT_TEMPLATES: dict[FailureClass, dict[str, tuple[str, ...]]] = {
    FailureClass.BANK_TIMEOUT: {
        "easy": (
            "ISSUER_TIMEOUT: no response from bank within 30000ms",
            "gateway error 504 upstream deadline exceeded",
            "Transaction timed out at issuer; please retry",
            "err=GATEWAY_TIMEOUT bank=HDFC latency_ms=30001",
        ),
        "hard": (
            "U69 payer psp unavailable, txn dropped at switch",
            "RC91 issuer inoperative",
        ),
    },
    FailureClass.WRONG_PIN: {
        "easy": (
            "Incorrect MPIN entered by customer",
            "invalid OTP supplied, attempts exceeded",
            "authentication failed at issuer 2FA step",
            "wrong pin - customer entered incorrect credentials",
        ),
        "hard": (
            "ZM validation of credential failed at npci",
            "cred block mismatch 3 of 3",
        ),
    },
    FailureClass.INSUFFICIENT_BALANCE: {
        "easy": (
            "Insufficient funds in customer account",
            "DECLINE: low balance, available 412.00 required 2199.00",
            "NSF - not enough funds to complete debit",
            "amount exceeds balance available in a/c",
        ),
        "hard": (
            "U30 debit failed at payer bank, drawdown refused",
            "RC51 do not honour - funding",
        ),
    },
    FailureClass.NETWORK: {
        "easy": (
            "network unreachable while contacting acquirer",
            "connectivity lost mid-transaction, socket closed",
            "DNS resolution failure for issuer endpoint",
            "link down between switch and issuer",
        ),
        "hard": (
            "BT tcp reset during authorisation leg",
            "switch leg aborted, no ack from downstream",
        ),
    },
    FailureClass.ACCOUNT_BLOCKED: {
        "easy": (
            "Customer account is blocked by issuing bank",
            "a/c frozen - debit freeze in place",
            "account restricted, lien marked",
            "closed account, cannot process debit",
        ),
        "hard": (
            "RC62 restricted card product",
            "payer vpa deregistered at psp",
        ),
    },
}

SOURCES = ("payments_stream", "subscription_schedule")


@dataclass(frozen=True, slots=True)
class Fixture:
    """A batch, its hidden truth, and everything needed to replay it exactly."""

    id: str
    seed: int
    items: tuple[AtRiskItem, ...]
    world: World
    ground_truth: dict[str, GroundTruth]

    @property
    def size(self) -> int:
        return len(self.items)

    @property
    def total_at_risk(self) -> float:
        return sum(i.amount for i in self.items)

    @property
    def recoverable_value(self) -> float:
        return sum(
            t.amount for t in self.ground_truth.values() if t.is_recoverable
        )


def _weighted_class(rng: random.Random) -> FailureClass:
    classes = list(POOL_MIX)
    weights = [POOL_MIX[c] for c in classes]
    return rng.choices(classes, weights=weights, k=1)[0]


def _gateway_text(rng: random.Random, cls: FailureClass) -> str:
    bucket = "hard" if rng.random() < 0.25 else "easy"
    return rng.choice(TEXT_TEMPLATES[cls][bucket])


def _amount(rng: random.Random) -> float:
    # Long-tailed, as merchant order values are: a median in the hundreds with
    # a thin tail of orders worth chasing hard.
    value = min(rng.lognormvariate(6.6, 1.05), 200_000.0)
    return round(value, 2)


def generate(
    seed: int = DEFAULT_SEED, size: int = DEFAULT_BATCH_SIZE
) -> Fixture:
    rng = random.Random(seed)
    customer_pool = ["cust_%04d" % n for n in range(int(size * 0.62) or 1)]

    items: list[AtRiskItem] = []
    latents: dict[str, Latent] = {}

    for index in range(size):
        item_id = "itm_%05d" % index
        true_class = _weighted_class(rng)
        occurred_at = BATCH_START + timedelta(
            minutes=rng.randrange(0, BATCH_DAYS * 24 * 60)
        )
        source = SOURCES[0] if rng.random() < 0.82 else SOURCES[1]
        item = AtRiskItem(
            id=item_id,
            type=(
                ItemType.PAYMENT_FAILURE
                if source == SOURCES[0]
                else ItemType.SUBSCRIPTION_DUE
            ),
            amount=_amount(rng),
            currency="INR",
            merchant_id="mrc_0001",
            customer_id=rng.choice(customer_pool),
            occurred_at=occurred_at,
            source=source,
            raw_gateway_context=_gateway_text(rng, true_class),
            prior_attempts=rng.choices([0, 1, 2], weights=[0.72, 0.22, 0.06])[0],
            prior_contacts=rng.choices([0, 1, 2], weights=[0.80, 0.16, 0.04])[0],
        )
        items.append(item)
        latents[item_id] = draw_latent(rng, item_id, true_class, occurred_at)

    world = World(latents)
    ground_truth = build_ground_truth(items, world)
    return Fixture(
        id="fx-%d-%d" % (seed, size),
        seed=seed,
        items=tuple(items),
        world=world,
        ground_truth=ground_truth,
    )
