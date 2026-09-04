# Recovery Desk

**Razorpay AI Buildathon 2026 · Track 3: AI Revenue Recovery**

Recovery Desk finds revenue slipping away from a merchant, decides which of it is
worth chasing under a finite recovery budget, executes bounded recovery actions,
and proves what it recovered against honest baselines.

The distinguishing claim is not that it recovers revenue. It is that it knows
which revenue *not* to chase, and can show the arithmetic.

> Roughly a third of payment failures are structurally unrecoverable. An agent
> that treats all failures as equally worth chasing burns money on outreach that
> could never have worked, and burns customer patience alongside it. **Recovery is
> an allocation problem under constraint, not a messaging problem.**

---

## Run it

No install step, no virtualenv, no API key. Python 3.11+.

```bash
python run.py demo
```

`make demo`, `make eval` and `make test` do the same via the Makefile where
`make` is available.

---

## The result

One seeded batch of 1,000 at-risk items worth **₹1,335,266**, of which
**₹779,931 (58.4%)** is genuinely recoverable by some action. Every arm gets the
same **₹2,500** budget, the same world, the same seed and the same policy gate.
Only the allocation policy differs.

| arm | policy | gross | net | rate | wasted | precision | spend | cost/₹ | violations |
|-----|--------|------:|----:|-----:|-------:|----------:|------:|-------:|-----------:|
| B0 | Do nothing | 0 | 0 | 0.0% | 0 | – | 0 | – | 0 |
| B1 | Blanket retry ×3 | 174,082 | 171,583 | 22.3% | 2,142 | 58.2% | 2,499 | 0.0144 | 0 |
| B2 | Rules-only heuristic | 461,983 | 459,576 | 59.2% | 1,480 | 61.5% | 2,408 | 0.0052 | 0 |
| **B3\*** | **Recovery Desk (no model)** | **526,924** | **524,424** | **67.6%** | **1,288** | **64.2%** | **2,500** | **0.0047** | **0** |

`fx-20260905-1000`, `policy-v1`. Reproduce with `python run.py demo`.

**Read this honestly.** B2 — a static rules heuristic with no model and no
expected-value arithmetic — captures most of the result. The desk's genuine
margin over it is **+14.1% net recovered**, **13% less wasted spend** and
**+2.7 points of chase precision**, at a p95 decision latency of 0.19 ms. That
is the real number. The first version of this comparison showed a 37-point gap,
and that was a bug in the baseline, not a result — see
[F2 in the failure log](docs/failures.md).

**B3 (with the model) has not been run.** No `ANTHROPIC_API_KEY` was set during
this build, so no claim is made about what the model contributes. The arm exists,
the ablation is wired, and `python run.py demo --model` measures it. Until it
runs, the ablation line reads *not run* rather than a number.

### What was deliberately not chased

263 of 1,000 items, worth ₹145,324. This is a headline, not a footnote.

| reason | items | value |
|--------|------:|------:|
| budget exhausted — spent on higher-yield items | 213 | ₹92,653 |
| unrecoverable class — account blocked or frozen | 38 | ₹50,999 |
| negative EV — the best action loses money | 11 | ₹1,502 |
| policy blocked | 1 | ₹170 |

---

## Architecture

Five stages, one direction of flow. Each has a single responsibility and a typed
boundary, so any stage can be swapped or ablated without touching the others.

```
   transaction stream  +  subscription schedule
                 |
        [ 1  INGEST ]        ->  AtRiskItem[]
                 |
        [ 2  DIAGNOSE ]      ->  failure class + recovery prior
                 |
        [ 3  DECIDE ]        ->  EV rank, allocate under budget
                 |               (incl. the option: do nothing)
          < policy gate >         caps · dedupe · idempotency
                 |
        [ 4  ACT ]           ->  retry / reroute / notify / escalate
                 |
        [ 5  PROVE ]         ->  scored vs B0 / B1 / B2 / B3
```

### The decision core

For each item and each candidate action:

```
EV(item, action) = P(recover | item, action) × amount × margin
                 - cost(action)
                 - λ × contact_fatigue(customer, action)
```

Every term is kept separately all the way into the audit log, because the
arithmetic *is* the product. Selection is greedy on EV-per-rupee-spent — O(n log n),
explainable line by line, and it produces a trace a human can audit. A solver
would buy a little optimality at the cost of being able to say exactly why item
447 was chosen and item 448 was not.

"Do nothing" is a first-class action with cost zero and EV zero. Any item whose
best action scores below it is suppressed, with the reason recorded.

---

## Where AI belongs, and where it does not

| Concern | Owner | Reasoning |
|---|---|---|
| Retry eligibility and backoff windows | **Rules** | Issuer policy is fixed and knowable; behaviour must be predictable and auditable |
| Spend caps, contact caps, deduplication | **Rules** | Safety must never depend on a sampler |
| Idempotency | **Rules** | A correctness primitive, not a judgement call |
| Failure classification from raw gateway text | **Model** | Unbounded text with a long tail of formats; regexes rot |
| Recovery probability | **Both** | Published base rates as the prior; model adjusts for messy context |
| Action selection | **Rules** | Deterministic arithmetic given the inputs; must be explainable |
| Customer message drafting | **Model** | Tone, language and localisation across Indian languages |
| Edge-case escalation | **Model proposes, rules gate** | Judgement where rules are brittle — but never unbounded |
| Audit narrative for humans | **Model** | Explanation is a language task |
| **Money movement** | **Rules only** | Non-negotiable. No model output initiates a debit or refund. |

The ablation exists so this table is testable rather than asserted: `B3 − B3*`
runs the identical pipeline with the model classifier replaced by its
deterministic fallback, and states the difference as a number.

---

## Guardrails

- **Dry-run is the only mode.** There is no code path from a decision to a real
  debit or a real message. This is structural, not a flag.
- Maximum retries per item, enforced *before* dispatch.
- Maximum contacts per customer per rolling window, across all channels.
- Total spend ceiling, checked before each commitment rather than reconciled after.
- An idempotency key on every action, so a replayed batch cannot double-charge or
  double-message.
- A closed enumeration of permitted actions. Any proposed action outside the set
  is rejected at the gate, logged and counted.
- A kill switch that halts dispatch mid-batch and leaves the ledger consistent.

### Explicit refusals

The system is designed **not** to do the following:

- It will not split transaction amounts to fall below an authentication
  threshold. That is regulatory evasion.
- It will not suppress, delay or obscure a customer's opt-out.
- It will not misrepresent urgency, consequences or account status in a recovery
  message.
- It will not retry against an account flagged as blocked or disputed. Asserted
  by `test_blocked_accounts_are_never_chased`.

---

## The evaluation harness

A seeded generator produces 1,000 at-risk items with known ground truth: for each
item, what would have happened under each candidate action, at the most
favourable moment that action could have been taken.

### The circularity firewall

If the desk could read the variables the simulator decides outcomes with, a good
score would prove only that both sides share a spreadsheet. The two sets are
disjoint:

| Latent — simulator only | Observable — desk only |
|---|---|
| `true_class` | `raw_gateway_context` (noisy text) |
| `balance_recovers_at` | `amount`, `occurred_at` |
| `issuer_outage_ends_at` | `prior_attempts`, `prior_contacts` |
| `customer_patience` | `customer_id`, `merchant_id` |
| `rail_affinity`, `draws` | |

`test_circularity_firewall` asserts this against the actual dataclass fields, not
against this table.

One fact is shared on purpose and lives in `calendar_facts.py`: when Indian
salary credits typically land. The desk observes that calendar; it never observes
`balance_recovers_at`, which is drawn with roughly 30 hours of jitter around it.
That gap is where the salary-cycle rule earns its keep or fails to.

The failure cause reaches the desk only as raw gateway text, a quarter of which
carries no keyword any matcher knows (`U30 debit failed at payer bank, drawdown
refused`). That is the headroom a model can occupy, and the ablation measures
whether it does.

### Baselines

| ID | Baseline | Why it is here |
|---|---|---|
| B0 | Do nothing | The floor. How much is genuinely at stake. |
| B1 | Blanket retry ×3, fixed backoff | What most merchants actually do. |
| B2 | Rules-only heuristic | The best static policy with no model. **The honest bar.** |
| B3* | Recovery Desk, no model | EV allocation on the deterministic classifier. |
| B3 | Recovery Desk | EV allocation on the model classifier. |

Every arm shares the budget, the world, the seed and the gate. Arms plan in
waves and only ever see items still unrecovered, so no arm is charged for
retries a real merchant would never have sent.

**A stated constraint:** an equal ₹2,500 across all arms means B1 gets roughly
one full retry wave over 1,000 items rather than three — three waves would cost
₹9,000. That is the thesis (same budget, different allocation) rather than a
handicap, but it is stated here rather than left to be discovered.

### Metrics

| Metric | Definition |
|---|---|
| Gross recovered | Total value of items successfully recovered |
| Net recovered | Gross recovered minus all action costs incurred |
| Recovery rate | Share of *genuinely recoverable* value captured |
| Wasted spend | Cost expended on items that never recovered |
| Chase precision | Share of items chosen for action that were actually recoverable |
| Contacts per rupee recovered | Proxy for customer annoyance; lower is better |
| Cost per rupee recovered | The efficiency headline |
| p95 decision latency | Throughput evidence at batch scale |
| Policy violations | **Must be zero.** Any non-zero value is a build failure. |

Every scored run appends to [`results/runs.csv`](results/runs.csv), including the
runs that got worse.

---

## Honest limits

Named here rather than left to be found. The full list is in
[docs/failures.md](docs/failures.md).

- **B3 has not been measured.** No API key during the build; the ablation reads
  *not run*.
- **The desk under-forecasts itself** — ₹417k expected recoverable against ₹527k
  actually recovered. Its priors are conservative relative to the world it is
  scored against, and the calibration gap is not yet quantified per class.
- **Salary-cycle timing is right on average, not per customer.** Items whose
  credit lands late are chased on the wrong day and counted as misses.
- **`UNKNOWN` items are priced pessimistically and mostly suppressed**, so the
  deterministic arm's blind spot appears as suppression rather than error, which
  flatters its precision.
- **The failure-mix distribution rests on a single industry source.** Marked
  `UNVERIFIED` in `diagnose/priors.py`. It shapes the fixture mix only, and no
  figure from it is claimed as fact.

---

## Layout

```
README.md          architecture, results, honest limits
Makefile           demo / eval / test
run.py             the same, with no make on PATH
src/recovery_desk/
  models.py        the six entities, immutable
  config.py        every knob, versioned
  calendar_facts.py  the one fact both sides share
  contract.py      engine -> UI, the only place presentation data is made
  ingest/          stream and schedule adapters
  diagnose/        classifier + deterministic fallback + published priors
  decide/          EV model, allocator, policy gate, baseline arms
  act/             dispatch, idempotency ledger, audit log
  prove/           harness, metrics, reports
  fixtures/        seeded generator + the outcome oracle
results/           versioned run table (committed)
docs/
  design.md        the source-of-truth design document
  decisions.md     why each boundary is where it is
  failures.md      the build failure log
tests/             six invariants, not a test suite
```

`python run.py demo` regenerates `results/reports/<run>/` — `run.json`,
`decisions.json` and `audit.json`, the versioned contract any surface reads.
Nothing downstream of `contract.py` may compute a decision or invent a number.
