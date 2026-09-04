"""Six invariants. Deliberately not a test suite.

These are the properties that, if they broke silently, would make every number
this repository prints meaningless. Everything else is checked by running the
harness and reading the result table.
"""

from __future__ import annotations

import sys
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recovery_desk.act.ledger import Ledger, idempotency_key
from recovery_desk.config import Policy
from recovery_desk.diagnose.classifier import DeterministicClassifier
from recovery_desk.fixtures.generator import generate
from recovery_desk.fixtures.world import Latent, latent_field_names, observable_field_names
from recovery_desk.models import ActionType, AtRiskItem, DecisionStatus, Outcome
from recovery_desk.prove.harness import run_all, run_arm
from recovery_desk.decide.strategies import RecoveryDesk

FIXTURE = generate(seed=7, size=250)
POLICY = Policy(budget=800.0)


def _desk():
    return run_arm(FIXTURE, RecoveryDesk(), POLICY, DeterministicClassifier())


def test_circularity_firewall():
    """The desk must not be able to read anything the simulator decides with.

    Asserted against the actual dataclass fields, not against a comment, so the
    day someone adds a latent to AtRiskItem this fails instead of quietly
    turning the evaluation into a lookup of the answer key.
    """
    assert latent_field_names().isdisjoint(observable_field_names())

    # And the observable item must carry no attribute of the latent by any name.
    latent_names = {f.name for f in fields(Latent)} - {"item_id"}
    for name in latent_names:
        assert not hasattr(FIXTURE.items[0], name), name


def test_budget_ceiling_is_never_exceeded():
    """Checked before each dispatch, not reconciled after the batch."""
    for result in run_all(FIXTURE, POLICY):
        assert result.ledger.spend <= POLICY.budget + 1e-6, result.arm_id
        assert result.metrics.total_spend <= POLICY.budget + 1e-6, result.arm_id


def test_blocked_accounts_are_never_chased():
    """An explicit refusal: no action against an account the issuer has frozen."""
    result = _desk()
    for decision in result.decisions:
        diagnosis = result.diagnoses[decision.item_id]
        if diagnosis.failure_class.value == "account_blocked_or_frozen":
            assert decision.status is DecisionStatus.SUPPRESSED
            assert decision.chosen_action is ActionType.DO_NOTHING


def test_idempotency_keys_are_unique_and_replay_is_refused():
    """A replayed batch must not double-charge or double-message."""
    result = _desk()
    dispatched = [a for a in result.attempts if a.dispatched_at is not None]
    keys = [a.idempotency_key for a in dispatched]
    assert len(keys) == len(set(keys))

    # The same action in the same run resolves to the same key, and the ledger
    # refuses it the second time.
    ledger = Ledger(policy=POLICY)
    key = idempotency_key("run", "itm_1", ActionType.RETRY_NOW, 0)
    assert key == idempotency_key("run", "itm_1", ActionType.RETRY_NOW, 0)
    assert ledger.claim(key) is True
    assert ledger.claim(key) is False
    assert ledger.violations == 1


def test_metric_denominators():
    """The two ratios that are easy to compute over the wrong denominator.

    recovery_rate is over genuinely recoverable value, not over the whole pool;
    chase_precision is over items chased, not over items recovered.
    """
    result = _desk()
    m = result.metrics
    recoverable = sum(
        t.amount for t in FIXTURE.ground_truth.values() if t.is_recoverable
    )
    assert abs(m.recoverable_value - recoverable) < 1e-6
    assert recoverable < FIXTURE.total_at_risk  # some of the pool is hopeless

    recovered_ids = {a.item_id for a in result.attempts if a.outcome is Outcome.RECOVERED}
    chased_ids = {d.item_id for d in result.decisions if d.status is DecisionStatus.CHASE}
    assert m.items_chased == len(chased_ids)
    assert m.items_recovered == len(recovered_ids)
    assert 0.0 <= m.chase_precision <= 1.0


def test_runs_are_deterministic():
    """Same seed, same world, same numbers. Without this nothing is comparable."""
    first = run_arm(FIXTURE, RecoveryDesk(), POLICY, DeterministicClassifier())
    second = run_arm(FIXTURE, RecoveryDesk(), POLICY, DeterministicClassifier())
    assert first.metrics.gross_recovered == second.metrics.gross_recovered
    assert first.metrics.items_chased == second.metrics.items_chased
    assert first.metrics.policy_violations == 0
