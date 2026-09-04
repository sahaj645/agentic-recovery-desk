# Recovery Desk — System Design Document

**Author** Sahaj Gaur · **Version** v0.1, design lock, pre-build ·
**Razorpay AI Buildathon 2026, Track 3: AI Revenue Recovery**

The authoritative document is [`Recovery-Desk-Design.pdf`](Recovery-Desk-Design.pdf)
in this directory. This file is the working extract the implementation is built
against — the parts the code is answerable to. Where the two disagree, the PDF
wins, and any deliberate deviation is recorded in [`decisions.md`](decisions.md).

## 1 · Thesis

Recovery Desk finds revenue slipping away, decides which of it is worth chasing
under a finite budget, executes bounded actions, and proves what it recovered
against honest baselines. The distinguishing claim is that it knows which revenue
*not* to chase, and can show the arithmetic.

Razorpay's Agent Studio already ships Abandoned Cart Conversion, Subscription
Recovery, Dispute Responder and Cashflow Forecaster. This design deliberately
avoids all four: rebuilding one means submitting a weaker copy of a product the
judges shipped. Those agents are point solutions; nothing above them decides
where a merchant's finite recovery effort should go, and nothing measures whether
any of it worked. That is the gap.

## 2 · The problem model

At-risk revenue is a heterogeneous pool, and recoverability is not uniform.

| Failure class | Share | Recoverability | Correct action |
|---|---|---|---|
| Bank / PSP timeout (technical decline) | 35–45% | High | Fast retry, then alternate rail |
| Wrong PIN / attempts exceeded | 20–30% | Low by retry | Offer another method; do not chase |
| Insufficient balance | 15–25% | Time-dependent | Retry on the salary cycle, not on backoff |
| Network / connectivity | 10–15% | Moderate | Short-delay retry |
| Account blocked or frozen | 5–10% | None | Suppress — spending here is pure waste |

Industry smart-retry logic recovers roughly 20–30% of timeout failures. That is
the bar, and it is why a naive retry baseline must appear in the evaluation.

**The salary-cycle insight.** Insufficient-balance failures are usually treated
as a fixed exponential-backoff problem. They are not. Balance is a calendar
phenomenon: it recovers on payday. A retry scheduled against salary-credit
patterns should materially outperform one scheduled against an exponential curve.
This is the sharpest rule in the system, specific to the Indian market, and
directly measurable against the naive baseline.

**Sourcing.** The distribution above rests on a single industry source. A second
independent source is required before any figure appears on camera or in the
README. Marked `UNVERIFIED` in `diagnose/priors.py`.

## 3 · Non-goals

Not built, and why: abandoned cart conversion, subscription dunning, dispute
response and cash-flow forecasting (Razorpay ships all four); fraud scoring
(Track 2, no credible labelled data); a merchant-side e-mandate notification
channel (the pre-debit notification is the issuer's obligation).

Inside scope: one leak class implemented deeply (failed payment attempts); the
agent proposes and executes bounded actions but never moves money autonomously;
no interface beyond a results view.

## 4 · Five stages

| # | Stage | Responsibility | Output |
|---|---|---|---|
| 1 | Ingest | Pull at-risk items, normalise to a common shape | `AtRiskItem[]` |
| 2 | Diagnose | Classify failure cause from raw gateway text; attach a recoverability prior | `Diagnosis` |
| 3 | Decide | Enumerate candidate actions, price them, rank by EV, allocate under budget and policy caps | `Decision` |
| 4 | Act | Dispatch idempotently through the policy gate; record the outcome | `ActionAttempt` |
| 5 | Prove | Score against baselines; emit metrics, regressions and the exception list | `BatchReport` |

Stage 5 is not bolted on at the end. It is the reason the product exists.

## 5 · The decision core

```
EV(item, action) = P(recover | item, action) × amount × margin
                 - cost(action)
                 - λ × contact_fatigue(customer, action)
```

- **P(recover)** — base rate by failure class, adjusted for amount band, hour of
  day, position in the salary cycle, and prior attempts.
- **amount × margin** — the item's value to the merchant, not its gross value.
  Recovering a ₹1,000 order at 20% margin is worth ₹200 of effort, not ₹1,000.
- **cost(action)** — real, configurable: gateway fee, message cost, per-minute
  voice cost.
- **contact_fatigue** — a penalty growing with each touch on the same customer.
  This is how the system prices the customer relationship rather than ignoring it.

Given budget B, select the action set maximising total EV subject to: total spend
≤ B, no customer above the contact cap in a rolling window, no transaction above
its retry cap. Selection is greedy on EV-per-rupee — chosen over a solver because
the decision trace must be auditable line by line.

"Do nothing" is a first-class action with cost zero and EV zero. The count of
deliberately suppressed items is a headline metric.

## 6 · AI vs deterministic

Reproduced verbatim in the README. Rules own retry eligibility, caps,
deduplication, idempotency, action selection and — non-negotiably — money
movement. The model owns failure classification from unbounded gateway text,
message drafting, edge-case escalation proposals and the audit narrative.
Recovery probability is shared: published base rates as the prior, model
adjustment for messy context.

An ablation is required: the harness must run the pipeline with model stages
replaced by deterministic fallbacks, so the model's contribution is a number
rather than an assertion.

## 7 · Guardrails

Dry-run by default. Retry cap per transaction and contact cap per customer,
enforced before dispatch. Spend ceiling checked before each dispatch. An
idempotency key on every action. A closed enumeration of permitted actions —
anything outside it is rejected at the gate, logged and counted. A kill switch
that halts mid-batch leaving the ledger consistent.

Every decision writes an audit row: the item, the diagnosis and its evidence,
every candidate action with its priced EV, the chosen action or suppression
reason, which component produced each input, the policy checks applied, and the
outcome.

**Explicit refusals.** No amount-splitting below authentication thresholds; no
suppressing or obscuring an opt-out; no misrepresenting urgency or account
status; no retry against a blocked or disputed account.

## 8 · Data model

| Entity | Fields |
|---|---|
| `AtRiskItem` | id · type · amount · currency · merchant_id · customer_id · occurred_at · source · raw_gateway_context |
| `Diagnosis` | item_id · failure_class · confidence · evidence · recovery_prior · classifier_provenance |
| `ActionCandidate` | item_id · action_type · estimated_cost · estimated_p_recover · earliest_executable_at |
| `Decision` | item_id · chosen_action \| suppressed · ev · full_ev_table · rationale · provenance · policy_checks_passed |
| `ActionAttempt` | decision_id · idempotency_key · dispatched_at · outcome · amount_recovered · cost_incurred |
| `BatchRun` | id · fixture_id · policy_version · budget · totals · metrics · started_at · finished_at |

Every entity is immutable once written. Corrections are new rows, not edits.

## 9 · The evaluation harness

A seeded generator produces at least 1,000 items with known ground truth — for
each item, what would have happened under each candidate action.

**The circularity trap.** The simulator's generative process must be independent
of the agent's feature set. If the agent scores well because it reads the same
variables the simulator used to decide outcomes, the number is meaningless. The
separation must be documented explicitly, and the panel will ask about it.

**Baselines.** B0 do nothing · B1 blanket retry ×3 fixed backoff · B2 rules-only
heuristic · B3 Recovery Desk. B2 is the one that matters: if the rules-only
policy captures most of the agent's result, that must be reported plainly, along
with an account of where the model earned its place.

**Metrics.** Gross recovered · net recovered · recovery rate · wasted spend ·
chase precision · contacts per rupee recovered · cost per rupee recovered · p95
decision latency · policy violations (must be zero).

**Regression tracking.** Every scored run appends to a versioned results table,
including the runs that got worse. A monotonic improvement curve reads as
fabricated.

## 10 · Failure taxonomy

A running log from day one, per entry: symptom · wrong hypothesis · root cause ·
fix · guard. The most valuable entries are those where an AI-generated
implementation looked correct and was not — a silent double-dispatch, an
idempotency key that was not unique, a metric over the wrong denominator. See
[`failures.md`](failures.md).

The system must also carry an honest list of unresolved exceptions. Claiming
near-total success invites a panel to find the hole; naming the hole first
removes the weapon.

## 11 · Reproduction

The README is the architecture document — the submission form has no field for
one. A judge spends sixty to ninety seconds deciding whether to watch the video,
so the repository must clone and run on a stranger's machine with a single
command, print the headline result table, and exit cleanly.

```
make demo    # seeded batch of 1,000 items, all baselines, results table
make eval    # full harness, regression table, exception list
make test    # unit and policy-gate tests
```

Repository hygiene: incremental commits with real messages; one consistent point
of view across modules; no dead abstractions or docstrings that contradict
behaviour. The submission is final and cannot be edited — the repository must be
quiet and complete before the form goes in.

## 12 · Assumptions to verify

| Assumption | How to kill it early |
|---|---|
| Razorpay test mode can simulate the needed failure cases | Spike the test-mode API on day 1 |
| The failure-mix distribution is accurate | Find a second independent source before any figure goes on camera |
| Real merchants will talk within two days | Start outreach on day 1 |
| The Claude Agent SDK fits the batch-processing shape | Build the thinnest end-to-end skeleton before any depth |

## 13 · Panel defence

Every boundary must be defensible from memory on a blank page. If any part cannot
be, cut it until it can.

| Likely question | The answer this design supports |
|---|---|
| Why is there a model here at all? | The ownership table in §6, then the ablation number |
| How do you know the synthetic data is not circular? | The simulator's causal variables are disjoint from the agent's features, asserted by a test |
| What if the model proposes a nonsense action? | The action space is a closed enumeration; anything outside it is rejected at the gate, logged and counted |
| Where did the model make things worse? | The regression table, including the runs that went backwards |
| How does this behave at ten million transactions a day? | The decision core is greedy and O(n log n); model calls occur only at diagnosis, batched and cached by failure signature |
| What is the cost of a false positive? | Wasted spend and contacts-per-rupee are headline metrics, not buried |
