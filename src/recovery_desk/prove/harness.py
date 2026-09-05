"""Stage 5. Run every arm over one identical world and score them.

This is the reason the product exists. Anyone can wire a model into a workflow;
the harness is what turns that into evidence. It exists so the question "is this
actually better than blanket retry?" has an answer with arithmetic behind it.

The arms share the fixture, the seed, the budget, the world and the ledger
discipline. Nothing differs between them except the allocation policy, so any
delta between two arms is attributable to that policy and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..act.audit import AuditRow, build_audit
from ..act.dispatch import dispatch
from ..act.ledger import Ledger
from ..config import Policy
from ..diagnose.classifier import Classifier, DeterministicClassifier, diagnose
from ..fixtures.generator import Fixture
from ..ingest import ingest
from ..models import (
    ActionAttempt,
    BatchRun,
    Decision,
    Diagnosis,
    Metrics,
    Outcome,
)
from ..decide.strategies import (
    BlanketRetry,
    DoNothing,
    RecoveryDesk,
    RulesOnly,
    Strategy,
)
from . import metrics as metrics_module


@dataclass(slots=True)
class ArmResult:
    """Everything one arm produced, kept whole so the UI never has to re-derive."""

    arm_id: str
    name: str
    description: str
    classifier_provenance: str
    metrics: Metrics
    decisions: list[Decision]
    attempts: list[ActionAttempt]
    audit: list[AuditRow]
    diagnoses: dict[str, Diagnosis]
    ledger: Ledger
    run: BatchRun
    #: Populated only when the classifier exposes .stats() (ModelClassifier).
    #: None for every deterministic arm -- there is nothing to report, and a
    #: missing dict is more honest than a dict of zeros that implies a model
    #: was involved when it never was.
    classifier_stats: dict | None = None


def run_arm(
    fixture: Fixture,
    strategy: Strategy,
    policy: Policy,
    classifier: Classifier,
    arm_id: str | None = None,
    name: str | None = None,
) -> ArmResult:
    """Plan, gate, dispatch and score one arm, wave by wave."""
    arm_id = arm_id or strategy.id
    run_id = "%s:%s" % (fixture.id, arm_id)
    started_at = datetime.now(timezone.utc)

    items = ingest(list(fixture.items))
    by_id = {item.id: item for item in items}

    diagnoses_list = diagnose(items, classifier)
    diagnoses = {d.item_id: d for d in diagnoses_list}

    # Persist whatever the classifier just learned. Without this, a model-backed
    # classifier's cache lives only in this process's memory: the next `demo`,
    # `ui` or `eval` invocation starts empty and re-attempts the same handful of
    # real calls from scratch, burning a free-tier rate limit for no reason and
    # quietly breaking the "a batch costs a few dozen calls" claim the moment two
    # commands run back to back. `hasattr` because the deterministic classifier
    # has no cache to flush.
    if hasattr(classifier, "flush"):
        classifier.flush()

    ledger = Ledger(policy=policy)
    all_decisions: list[Decision] = []
    all_attempts: list[ActionAttempt] = []
    all_latencies: list[float] = []
    recovered: set[str] = set()

    pending = list(items)
    for round_index in range(strategy.max_rounds):
        if not pending:
            break
        decisions, latencies = strategy.plan(
            round_index, pending, diagnoses, policy, ledger, run_id
        )
        attempts = dispatch(
            decisions, by_id, fixture.world, ledger, run_id, dry_run=policy.dry_run
        )

        all_decisions.extend(decisions)
        all_attempts.extend(attempts)
        all_latencies.extend(latencies)

        # A recovered item leaves the pool. A real merchant does not keep
        # retrying a payment that already went through, and neither does an arm.
        recovered |= {a.item_id for a in attempts if a.outcome is Outcome.RECOVERED}
        pending = [item for item in pending if item.id not in recovered]

    computed = metrics_module.compute(
        items, all_decisions, all_attempts, fixture.ground_truth,
        ledger, all_latencies, policy,
    )

    attempts_by_decision = {a.decision_id: a for a in all_attempts}
    audit = build_audit(
        all_decisions,
        diagnoses,
        attempts_by_decision,
        {i.id: i.customer_id for i in items},
        {i.id: i.amount for i in items},
    )

    finished_at = datetime.now(timezone.utc)
    run = BatchRun(
        id=run_id,
        arm=arm_id,
        fixture_id=fixture.id,
        policy_version=policy.version,
        budget=policy.budget,
        metrics=computed,
        started_at=started_at,
        finished_at=finished_at,
    )

    return ArmResult(
        arm_id=arm_id,
        name=name or strategy.name,
        description=strategy.description,
        classifier_provenance=classifier.provenance,
        metrics=computed,
        decisions=all_decisions,
        attempts=all_attempts,
        audit=audit,
        diagnoses=diagnoses,
        ledger=ledger,
        run=run,
        classifier_stats=(
            classifier.stats() if hasattr(classifier, "stats") else None
        ),
    )


def run_all(
    fixture: Fixture,
    policy: Policy,
    model_classifier: Classifier | None = None,
) -> list[ArmResult]:
    """Run B0, B1, B2, the ablation arm, and -- if a model is wired -- B3.

    The ablation arm is the same desk on the deterministic classifier. Without
    it, any claim about what the model contributed is an assertion.
    """
    rules = DeterministicClassifier()

    results = [
        run_arm(fixture, DoNothing(), policy, rules),
        run_arm(fixture, BlanketRetry(), policy, rules),
        run_arm(fixture, RulesOnly(), policy, rules),
        run_arm(
            fixture,
            RecoveryDesk(),
            policy,
            rules,
            arm_id="B3*",
            name="Recovery Desk (no model)",
        ),
    ]

    if model_classifier is not None:
        results.append(
            run_arm(
                fixture,
                RecoveryDesk(),
                policy,
                model_classifier,
                arm_id="B3",
                name="Recovery Desk",
            )
        )

    return results


def ablation_delta(results: list[ArmResult]) -> dict[str, float] | None:
    """What the model bought, in rupees. None when the model arm did not run."""
    by_id = {r.arm_id: r for r in results}
    if "B3" not in by_id or "B3*" not in by_id:
        return None
    model, rules = by_id["B3"].metrics, by_id["B3*"].metrics
    return {
        "net_recovered": model.net_recovered - rules.net_recovered,
        "gross_recovered": model.gross_recovered - rules.gross_recovered,
        "wasted_spend": model.wasted_spend - rules.wasted_spend,
        "chase_precision": model.chase_precision - rules.chase_precision,
    }
