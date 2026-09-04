# Why each boundary is where it is

The design document is the source of truth; this file records the calls made
while implementing it, including the two places the implementation deviates from
the document and why.

## Greedy, not an optimiser

Selection is greedy on EV-per-rupee. A knapsack solver would buy a small amount
of optimality and cost the property that matters most: being able to say exactly
why item 447 was chosen and item 448 was not. The allocator is O(n log n), every
step is a comparison a human can redo on paper, and the full priced table for
every item survives into the audit log.

## Two passes, not one

Pass A prices every action for every item and drops the ones that lose money.
Pass B walks the survivors in EV-per-rupee order and re-prices against the live
ledger before committing. The re-pricing exists because contact fatigue and the
remaining budget both move as the desk spends. Ranking on a stale number is
acceptable; spending on one is not.

## Reservation at decision time, not dispatch time

`Ledger.reserve()` debits the budget when the desk decides, not when it
dispatches. A ceiling enforced at dispatch lets the allocator promise the same
rupee to two items and discover the overspend afterwards, which is not a ceiling.
Idempotency is a separate concern with a separate method — see failure F4.

## Outcomes drawn per action family

The simulator draws one uniform for retries and one for contacts, not one per
action. Recoverability is mostly a property of the item: if the customer has no
funds no rail helps, and if they have no intent no channel lands. Independent
per-action draws handed every item six lottery tickets and inflated the
recoverable pool to 85% — see failure F1.

## The shared calendar is explicit

`calendar_facts.py` exists so that the one fact both sides legitimately share —
when Indian salary credits typically land — is a named module rather than an
import from the simulator into the decision core. The desk observes the
calendar. It never observes `balance_recovers_at`, which is drawn with jitter
around it. That gap is where the salary-cycle rule earns its keep or fails to.

## Equal budget across all arms

Every arm gets the same ₹2,500. This is the sharpest available framing of the
thesis — same budget, different allocation — but it does constrain B1: three
full retry waves over 1,000 items would cost ₹9,000, so under an equal budget B1
gets roughly one wave. That is a deliberate choice and it is stated in the
README rather than left for a judge to notice. An unbounded-B1 diagnostic would
answer "what would blanket retry cost to match this?" and has not been built.

## Dry-run only, with no live path

There is no code path from a decision to a real debit or a real message. Live
dispatch is not disabled behind a flag; it does not exist. The bound is
structural rather than configurational.

## `UNKNOWN` is priced pessimistically

An unreadable gateway response blends the class prior toward the unknown prior in
proportion to classifier confidence. Uncertainty should make the desk spend less,
not guess more. The side effect is that the deterministic arm's blind spot shows
up as suppression rather than as error, which flatters its precision — recorded
in the exception list.

## Deviations from the design document

**1. The fixture generator lives in `src/recovery_desk/fixtures/`, not in a
top-level `fixtures/`.** The document's layout puts the generator at the
repository root. A root-level package is not importable from an installed
distribution without extra packaging configuration, and the one-command-clone
property matters more than the directory diagram. The root `fixtures/` directory
remains for generated artefacts.

**2. There are five arms, not four.** The document specifies B0–B3 and separately
requires an ablation that runs the pipeline with model stages replaced by their
deterministic fallbacks. Those are different things: B2 is a static heuristic
with no EV arithmetic at all, while the ablation is the full desk on the
deterministic classifier. Both are needed, so the harness runs `B3*` (desk, no
model) alongside `B3` (desk, model). `B3 − B3*` is the model's contribution;
`B3* − B2` is the allocation engine's.

## Not built, deliberately

- No UI beyond the results view and the JSON contract that feeds it.
- No live Razorpay API integration; the test-mode spike is a research task.
- No second leak class until the first is complete and scored.
- No broad test suite — six invariants, listed in `tests/test_invariants.py`.
- No voice dispatch beyond pricing and a value threshold at the gate.
