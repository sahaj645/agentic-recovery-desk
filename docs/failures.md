# Build failure log

Kept from day one, one entry per thing that broke. The format is fixed:
symptom, the wrong hypothesis, the root cause, the fix, and the guard that stops
it recurring silently.

The entries worth reading are the ones where the implementation looked correct
and was not. Every number in this repository was wrong at some point below.

---

## F1 — The recoverable pool was 85% recoverable

**Symptom.** The first fixture run reported that 85.5% of at-risk items were
recoverable by some action. The design's premise is that roughly a third of
payment failures are structurally unrecoverable. If 85% is recoverable, the
product has no thesis.

**Wrong hypothesis.** The class mix was wrong — too many timeouts, not enough
blocked accounts. Checking the mix showed it matched the intended distribution
almost exactly, so that was not it.

**Root cause.** The world drew an independent uniform for every one of the six
actions. Each item therefore got six independent chances to be recoverable, and
`P(at least one succeeds)` compounded to near-certainty even where each
individual probability was low. A wrong-PIN item with four sub-20% channels came
out 49% recoverable.

**Fix.** Outcomes are drawn per action *family* — one draw for retries, one for
contacts — because recoverability is mostly a property of the item, not of the
channel. If the customer has no funds, no rail helps; if they have no intent, no
message lands. Recoverable share fell to 58.6%, and 41% of the pool became
structurally unrecoverable, which is the shape the design predicted.

**Guard.** `ACTION_FAMILY` in `fixtures/world.py` is the single place the mapping
lives, and `test_metric_denominators` asserts the recoverable value is strictly
less than the pool.

---

## F2 — B2 was a straw man, and it flattered us by 33 points

**Symptom.** The first honest comparison had B2 (rules-only) recovering 26.8%
against the desk's 64.4%. A 37-point gap over a static policy is not a result,
it is a bug in the baseline.

**Wrong hypothesis.** That the desk's EV allocation was simply that much better.
It is better, but not by that much, and believing the first flattering number is
exactly the failure mode the evaluation exists to prevent.

**Root cause.** B2's static map retried technical declines 15 minutes after they
failed. Issuer outages in the simulator run 12 to 240 minutes, so almost every
B2 retry landed while the issuer was still down and drew the 0.05 branch. B2 was
not the best static policy; it was a policy with a timing bug.

**Fix.** B2 now waits a fixed four hours on technical and network declines —
what a competent engineer would actually write, and what smart-retry products do.
Its recovery rate went from 26.8% to 59.2%, and the desk's genuine margin over it
is +14.1% net recovered, not +140%.

**Guard.** `STATIC_ACTION` carries an explicit comment that B2 must stay the
strongest model-free policy, since a weak B2 makes the desk look good for free.

---

## F3 — The desk never contacted anyone, and lost the only class that needs it

**Symptom.** Across 1,000 items the desk chose a contact action 10 times. Wrong
PIN is 25% of the pool and is the one class recoverable *only* by reaching the
customer, so the desk was structurally abandoning a quarter of the pool.

**Wrong hypothesis.** That the contact priors were too low. They were not — a
WhatsApp nudge on a wrong-PIN failure carries a 0.22 prior, comfortably the best
available action for that class.

**Root cause.** `contact_fatigue` returned `(touches + 1) ** 2`, so a customer
who had never been contacted still cost one fatigue unit. At λ=25 that is ₹25 of
goodwill charged against an SMS costing ₹0.25, which priced every first contact
out of contention.

**Fix.** Fatigue is now `touches ** 2`: the first message is free, the second
costs λ, the third costs 4λ. One message about a failed payment is service; the
third is harassment, and the curve should say so. Recovery rate went 64.4% →
67.6%.

**Guard.** The docstring on `contact_fatigue` states the intent, and the fatigue
column is rendered in every decision trace, so a mispriced first touch is visible
on screen rather than buried in an aggregate.

---

## F4 — The ledger conflated reserving budget with claiming an idempotency key

**Symptom.** No visible failure. Found while wiring dispatch.

**Wrong hypothesis.** None — this was caught by reading, not by a red test,
which is why it is worth recording.

**Root cause.** A single `Ledger.record` both debited the budget and inserted the
idempotency key. The allocator called it at decision time with a synthetic
`"pending:"` key, so the real key inserted at dispatch was a different string.
The result would have been an idempotency set that never matched what was
actually dispatched — replay protection that silently protected nothing.

**Fix.** Split into `reserve()` (budget and cap headroom, at decision time) and
`claim()` (the key, at dispatch). Reservation has to happen at decision time
regardless: a budget debited only on dispatch lets the allocator promise the
same rupee to two items.

**Guard.** `test_idempotency_keys_are_unique_and_replay_is_refused` asserts both
that dispatched keys are unique and that a second claim is refused and counted.

---

## F5 — B1 was being charged for retries a real merchant would never send

**Symptom.** B1 spent its entire budget in the first wave, on items that had
already recovered.

**Wrong hypothesis.** That this was simply what a finite budget does to a
blanket-retry policy, and therefore fair.

**Root cause.** All three retry waves were planned up front, before any outcome
was known. A merchant retrying three times stops the moment one succeeds; the
harness was charging B1 for attempts nobody would have made, which biased the
comparison *in the desk's favour*.

**Fix.** Arms now plan in waves, and each wave only sees items still unrecovered.
Every arm shares the same loop, so the correction is structural rather than a
special case for B1.

**Guard.** The `Strategy` protocol takes `round_index` and a pending-items list,
so a new arm cannot accidentally plan against already-recovered items.

---

## F6 — Run directories could not be created on Windows

**Symptom.** `OSError: [WinError 123]` writing
`results/reports/fx-20260905-1000_B3*`.

**Root cause.** The ablation arm id is `B3*`, and `*` is not a legal filename
character on Windows. The id itself is right — it reads correctly in tables — so
the id was not the thing to change.

**Fix.** `_slug()` sanitises run ids for the filesystem while the display id
stays as it is.

**Guard.** None beyond the fix. Noted here because a repository that only runs on
the author's machine fails the one property judges test first.

---

## F7 — Pricing the premium rail correctly made the desk lose to rules-only

**Symptom.** After changing the alternate-rail cost from a flat rupee amount to
a percentage of the item's amount (so a big-ticket reroute is a real budget
commitment, not free), Recovery Desk's net recovered on the standard evaluation
fixture *dropped* from the previous run's number to below B2, the rules-only
baseline — the exact regression the evaluation harness exists to catch.

**Wrong hypothesis.** That the percentage-based cost itself was wrong, or too
aggressive, and needed tuning down.

**Root cause.** The allocator ranked items by their single best action's EV,
then spent by ranking on `EV / (cost + fatigue)` — a density computed once per
item using whichever action happened to have the highest raw EV. For a large
timeout, that was often the premium reroute, whose EV is high but whose density
is low once its real percentage-based cost is counted. Ranking the *item* by
that action's low density buried a genuinely excellent item (its own cheap
retry has an outstanding density) near the bottom of the spending order, so the
budget filled with weaker items first.

**Fix.** The allocator now solves for lambda, the budget's shadow price, and
every item takes whichever action maximises `EV - lambda * cost` — including
none, if even the best real action doesn't clear it. Ranking and spending both
use this single, correct criterion instead of a per-item density computed from
an arbitrarily chosen action. Net recovered against B2 went from worse to
**+21.3%**.

**Guard.** The evaluation harness catching this at all is the guard: the
regression was visible in `results/runs.csv` and in `python run.py eval`'s
headline table the moment it was run, not discovered later. No automated test
pins the exact recovery-rate number, since the fixture and priors are expected
to keep evolving; the invariant that *is* pinned is that `B3* net_recovered` is
computed the same way every arm's is, so a real regression cannot hide.

---

## F8 — The "smaller but better" case does not occur in the evaluation fixture

**Symptom.** Building an automated case-finder for the evidence report
(`prove/cases.py`), four of the five representative cases the design asks for
turned up immediately in the standard 1,000-item evaluation batch at its
₹2,500 budget. The fifth — a smaller payment funded while a larger one is
outbid — never did, across every seed tried.

**Wrong hypothesis.** That the case-finder's search was too narrow (wrong
amount ratio threshold, wrong suppression reason).

**Root cause.** It is not a search bug. At the evaluation's standard budget, an
unbiased 1,000-item draw's total demand for positive-EV actions never exceeds
the budget by enough margin to force a genuine auction between a large item and
a small one — `budget_exhausted` essentially does not appear in the
suppression-reason breakdown at ₹2,500. Budget scarcity of the kind that
produces this case is real but statistically rare in an unbiased draw; it is
exactly why `fixtures/scenario.py` was built as a separate, constructed batch
in the first place (see `docs/decisions.md`).

**Fix.** The case-finder tries the evaluation fixture first, honestly. Only if
that comes up empty does it fall back to the constructed scarcity scenario,
and every case reports which fixture it came from — nothing is blended or
silently substituted.

**Guard.** `find_cases()`'s docstring states this explicitly, and the case
narrative for "smaller but better" names its own source fixture in the text a
reader sees, not just in a machine-readable field.

---

## Unresolved exceptions

Named here rather than left for someone else to find.

- **The desk under-forecasts itself.** Expected recoverable revenue comes out at
  ₹417k against ₹527k actually recovered. The priors are conservative relative to
  the simulator's outcome process. This is honest — the desk is not tuned to the
  world it is scored against, which is the point — but it means the forecast
  column on the overview reads low, and the calibration gap is not yet quantified
  per failure class.
- **Insufficient-balance timing is right on average, not per customer.** The desk
  schedules against the salary calendar; the simulator draws an individual credit
  date with roughly 30 hours of jitter around it. Items whose credit lands late
  are chased on the wrong day and are counted as misses.
- **`UNKNOWN` items are priced pessimistically and therefore mostly suppressed.**
  That is the safe default, but it means the deterministic arm's blind spot shows
  up as suppression rather than as error, which flatters its precision.
- **`decisions.json` is 4.5 MB for a 1,000-item batch.** Fine over localhost,
  not fine over a network. Splitting the candidate tables out of the queue
  document is the obvious fix and has not been done.
