"""A constructed scarcity scenario: the batch the demo and the video run on.

The statistical fixture in ``generator.py`` is the right thing to *score* on --
it is an unbiased draw and the baseline comparison lives on it. But an unbiased
draw of a thousand items does not reliably contain the handful of cases that
make the portfolio behaviour legible in ten seconds: the whale the desk walks
past, the small payment it funds instead, the frozen account it refuses to
touch. Those cases exist in the wild but are rare, and a demo cannot depend on a
lucky seed.

So this module *constructs the economics*. It plants a small set of designed
opportunities -- chosen amounts, failure signatures, prior-attempt and
prior-contact counts -- and surrounds them with a random remainder tilted toward
the classes where recovery is genuinely scarce (contact-only wrong-PIN,
retry-exhausted high-value). Every planted value is an **input**: the amount, the
gateway text, how many times it has already been retried. The allocator is never
told what to decide. It still prices every action, ranks by budget density, and
allocates under the budget -- and *because* the inputs are what they are, it
produces the memorable decisions on its own. Change the budget and the same
inputs produce a different selection, which is the whole point.

The firewall is intact: planted items carry only desk-visible fields, and their
latent outcomes are drawn by the same ``draw_latent`` as everything else.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from ..models import AtRiskItem, FailureClass, ItemType
from .generator import BATCH_START, TEXT_TEMPLATES, Fixture, _amount
from .world import World, build_ground_truth, draw_latent

#: A scenario item is fully specified by the fields the desk can see. Nothing
#: here touches the outcome oracle; the world still decides what recovers.
_MERCHANT = "mrc_0001"


def _text(cls: FailureClass, index: int = 0) -> str:
    return TEXT_TEMPLATES[cls]["easy"][index]


def _hero(
    item_id: str,
    label: str,
    amount: float,
    cls: FailureClass,
    occurred_at: datetime,
    customer_id: str,
    prior_attempts: int = 0,
    prior_contacts: int = 0,
    text_index: int = 0,
) -> AtRiskItem:
    return AtRiskItem(
        id=item_id,
        type=ItemType.PAYMENT_FAILURE,
        amount=amount,
        currency="INR",
        merchant_id=_MERCHANT,
        customer_id=customer_id,
        occurred_at=occurred_at,
        source="payments_stream",
        raw_gateway_context=_text(cls, text_index),
        prior_attempts=prior_attempts,
        prior_contacts=prior_contacts,
    )


def _planted(rng: random.Random) -> list[tuple[AtRiskItem, FailureClass, str]]:
    """The designed opportunities. Returns (item, true_class, storyline label).

    Each one is engineered to occupy a specific economic position, but only
    through its inputs. What the desk does with it is the desk's call.
    """
    t0 = BATCH_START + timedelta(days=2, hours=9)
    heroes: list[tuple[AtRiskItem, FailureClass, str]] = []

    # 1. THE WHALE THE DESK WALKS PAST.
    #    The largest payment in the batch, and the desk does nothing with it --
    #    because the issuer has frozen the account. Every rupee spent chasing a
    #    frozen account is pure waste, so the amount is irrelevant. This is the
    #    design's core claim made visible: a third of failures are structurally
    #    unrecoverable, and the desk's first job is to recognise them and stop.
    heroes.append((
        _hero("itm_h0001", "frozen-whale", 88000.0, FailureClass.ACCOUNT_BLOCKED,
              t0, "cust_whale"),
        FailureClass.ACCOUNT_BLOCKED,
        "whale",
    ))

    # 2. THE SMALL PAYMENT IT FUNDS INSTEAD.
    #    An eighth of the whale's size, but a fresh insufficient-balance failure
    #    whose salary-timed retry costs Rs2.50 at a high probability -- an
    #    enormous density. Funded early and gladly, while the frozen whale is
    #    left alone. Amount is not the story; recoverability-per-rupee is.
    heroes.append((
        _hero("itm_h0002", "small-winner", 11200.0, FailureClass.INSUFFICIENT_BALANCE,
              t0 + timedelta(hours=1), "cust_win01"),
        FailureClass.INSUFFICIENT_BALANCE,
        "winner",
    ))

    # 3. THE EXPENSIVE REROUTE THE DESK DECLINES.
    #    A large fresh timeout. Routing it through a premium rail has the highest
    #    expected value of any single action on it -- but it would cost ~Rs610,
    #    and the desk ranks the item by its cheap retry's density instead. Under a
    #    binding budget it fires the Rs2.50 retry and keeps the Rs610 for hundreds
    #    of other recoveries. "It did not pay to reroute your biggest failure."
    heroes.append((
        _hero("itm_h0003", "reroute-declined", 68000.0, FailureClass.BANK_TIMEOUT,
              t0 + timedelta(hours=2), "cust_rr01", text_index=1),
        FailureClass.BANK_TIMEOUT,
        "reroute-declined",
    ))

    # 4. THE HIGH-VALUE ITEM WITH EVERY LEVER SPENT.
    #    A large payment already retried to the cap and contacted to the cap. No
    #    action remains that is not throwing good money after bad, so the desk
    #    stops. Suppressed for a reason a human can defend: not "we gave up," but
    #    "there is nothing left that pays."
    heroes.append((
        _hero("itm_h0004", "levers-spent", 54000.0, FailureClass.BANK_TIMEOUT,
              t0 + timedelta(hours=3), "cust_spent01",
              prior_attempts=3, prior_contacts=2),
        FailureClass.BANK_TIMEOUT,
        "exhausted",
    ))

    # 5. DO NOTHING, BECAUSE THE CUSTOMER HAS HAD ENOUGH.
    #    A small wrong-PIN failure -- recoverable only by reaching the customer,
    #    who has already been contacted once this week. A second nudge costs more
    #    goodwill than the thin margin on a Rs180 order is worth, and a retry does
    #    nothing for a wrong PIN, so the optimal action is none.
    heroes.append((
        _hero("itm_h0005", "fatigued-optout", 180.0, FailureClass.WRONG_PIN,
              t0 + timedelta(hours=4), "cust_fat01", prior_contacts=1),
        FailureClass.WRONG_PIN,
        "fatigue",
    ))

    # 6. A HIGH-PROBABILITY MID-TICKET THAT SIMPLY EARNS ITS PLACE.
    #    Not a twist -- a control. A clean timeout with a cheap high-probability
    #    retry, exactly the kind of rupee the budget should buy first.
    heroes.append((
        _hero("itm_h0006", "clean-timeout", 8300.0, FailureClass.BANK_TIMEOUT,
              t0 + timedelta(hours=5), "cust_ok01"),
        FailureClass.BANK_TIMEOUT,
        "control",
    ))

    return heroes


#: The scenario tilts the class mix toward the classes where recovery is scarce.
#: Contact-only wrong-PIN and retry-exhausted high-value are where the premium
#: budget actually binds; a pool of easy timeouts would let the budget fund
#: everything and hide the allocation.
_SCENARIO_MIX: dict[FailureClass, float] = {
    FailureClass.BANK_TIMEOUT: 0.30,
    FailureClass.WRONG_PIN: 0.32,
    FailureClass.INSUFFICIENT_BALANCE: 0.22,
    FailureClass.NETWORK: 0.10,
    FailureClass.ACCOUNT_BLOCKED: 0.06,
}


def _scenario_class(rng: random.Random) -> FailureClass:
    classes = list(_SCENARIO_MIX)
    return rng.choices(classes, weights=[_SCENARIO_MIX[c] for c in classes], k=1)[0]


def _scenario_amount(rng: random.Random) -> float:
    """A fatter high-value tail than the statistical pool.

    The premium budget only binds when enough high-value items demand a premium
    action, so the scenario carries more big tickets than an unbiased draw.
    """
    base = _amount(rng)
    if rng.random() < 0.16:
        base *= rng.uniform(6.0, 22.0)  # the high-value tail that needs rerouting
    return round(min(base, 250_000.0), 2)


def generate_scenario(seed: int = 20260905, size: int = 420) -> Fixture:
    """Planted heroes first, then a scarcity-tilted random remainder."""
    rng = random.Random(seed)
    items: list[AtRiskItem] = []
    latents = {}

    heroes = _planted(rng)
    for item, true_class, _label in heroes:
        items.append(item)
        latents[item.id] = draw_latent(rng, item.id, true_class, item.occurred_at)

    customer_pool = ["cust_%04d" % n for n in range(int(size * 0.62) or 1)]
    for index in range(size - len(heroes)):
        item_id = "itm_%05d" % index
        true_class = _scenario_class(rng)
        occurred_at = BATCH_START + timedelta(minutes=rng.randrange(0, 30 * 24 * 60))
        prior_attempts = rng.choices([0, 1, 2, 3], weights=[0.55, 0.24, 0.14, 0.07])[0]
        prior_contacts = rng.choices([0, 1, 2], weights=[0.68, 0.22, 0.10])[0]
        item = AtRiskItem(
            id=item_id,
            type=ItemType.PAYMENT_FAILURE,
            amount=_scenario_amount(rng),
            currency="INR",
            merchant_id=_MERCHANT,
            customer_id=rng.choice(customer_pool),
            occurred_at=occurred_at,
            source="payments_stream",
            raw_gateway_context=TEXT_TEMPLATES[true_class][
                "hard" if rng.random() < 0.2 else "easy"
            ][rng.randrange(0, 2)],
            prior_attempts=prior_attempts,
            prior_contacts=prior_contacts,
        )
        items.append(item)
        latents[item.id] = draw_latent(rng, item.id, true_class, occurred_at)

    world = World(latents)
    ground_truth = build_ground_truth(items, world)
    return Fixture(
        id="scenario-%d-%d" % (seed, size),
        seed=seed,
        items=tuple(items),
        world=world,
        ground_truth=ground_truth,
    )


#: The storyline labels, exposed so the CLI and UI can point at each hero by the
#: role it plays -- without hardcoding what the allocator decided about it.
HERO_LABELS = {
    "itm_h0001": "The whale the desk walks past (frozen)",
    "itm_h0002": "The small payment it funds instead",
    "itm_h0003": "Reroute declined despite the amount",
    "itm_h0004": "The high-value item with every lever spent",
    "itm_h0005": "Do nothing: the customer has had enough",
    "itm_h0006": "A clean retry that earns its place",
}
