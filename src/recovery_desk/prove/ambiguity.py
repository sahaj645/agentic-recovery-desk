"""Where AI could add information a deterministic classifier structurally cannot.

This module answers one question precisely: how much money sits behind gateway
text the rules-only classifier cannot place? It does not answer "how much would
AI recover" -- that requires an actual model call, measured by the ablation in
``harness.py`` and reported honestly even when the answer is zero. This module
computes a *ceiling*, not a result.

The ceiling is computed against the simulator's ground truth, which is exactly
the thing the desk itself is never allowed to see (the circularity firewall in
``fixtures/world.py``). That is safe here for the same reason it is safe in
``prove/cases.py``: this function runs after the batch is scored, for a human
evidence report, and never feeds back into a diagnosis, a probability, or a
decision. If it ever were imported from ``diagnose/`` or ``decide/``, that would
be the firewall breaking; ``tests/test_ai_adapter.py`` and
``test_invariants.py`` together pin every import in those two packages, and
this module deliberately lives in ``prove/`` -- evidence-only territory -- not
beside the classifier it evaluates.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..diagnose.classifier import Classifier
from ..fixtures.generator import Fixture
from ..models import AtRiskItem, FailureClass

#: Below this confidence, the rules-only classifier is telling you it does not
#: know, not that it has weighed the evidence and landed on UNKNOWN with
#: conviction. Both are worth measuring, so both counts are reported below.
LOW_CONFIDENCE_THRESHOLD = 0.5


@dataclass(frozen=True, slots=True)
class AmbiguityReport:
    """The size and value of the zone rules cannot resolve, and how AI did on it.

    ``uncertain_items`` / ``uncertain_value`` are what rules alone leave
    ambiguous: UNKNOWN outright, or a real class guessed at confidence below
    the threshold. ``addressable_recoverable_value`` is the ceiling -- the
    portion of that ambiguous value that ground truth says was genuinely
    recoverable, and therefore the most AI classification could ever be worth
    here, before EV, budget or policy touch it at all.

    ``model_resolved_items`` / ``model_correct_items`` are populated only when
    a live model classifier actually ran; they are the measured result, kept
    structurally separate from the ceiling above so the two can never be
    reported as the same number.
    """

    total_items: int
    uncertain_items: int
    uncertain_value: float
    unknown_items: int
    low_confidence_items: int
    addressable_recoverable_items: int
    addressable_recoverable_value: float
    model_ran: bool
    model_resolved_items: int
    model_correctly_classified_items: int
    model_still_uncertain_items: int


def _is_uncertain(diagnosis) -> bool:
    return (
        diagnosis.failure_class is FailureClass.UNKNOWN
        or diagnosis.confidence < LOW_CONFIDENCE_THRESHOLD
    )


def measure(
    fixture: Fixture,
    rules_diagnoses: dict[str, object],
    model_diagnoses: dict[str, object] | None = None,
) -> AmbiguityReport:
    """Quantify the ambiguity zone from the rules-only classifier's output.

    Pass ``model_diagnoses`` (a real model classifier's output on the same
    fixture) to additionally measure how many of those same uncertain items the
    model actually resolved, and how many of its resolutions matched ground
    truth. Omit it, or pass diagnoses where the model never ran (100%
    fallback), and those three fields stay honestly at zero -- a model that was
    never really invoked cannot be credited with resolving anything.
    """
    truth = fixture.ground_truth
    amounts = {i.id: i.amount for i in fixture.items}

    uncertain_ids: set[str] = set()
    unknown_count = 0
    low_confidence_count = 0
    uncertain_value = 0.0

    for item_id, diagnosis in rules_diagnoses.items():
        if diagnosis.failure_class is FailureClass.UNKNOWN:
            unknown_count += 1
        elif diagnosis.confidence < LOW_CONFIDENCE_THRESHOLD:
            low_confidence_count += 1
        else:
            continue
        uncertain_ids.add(item_id)
        uncertain_value += amounts[item_id]

    addressable_items = [i for i in uncertain_ids if truth[i].is_recoverable]
    addressable_value = sum(amounts[i] for i in addressable_items)

    model_resolved = 0
    model_correct = 0
    model_still_uncertain = 0
    model_ran = False

    if model_diagnoses:
        for item_id in uncertain_ids:
            model_dx = model_diagnoses.get(item_id)
            if model_dx is None:
                continue
            # A model diagnosis whose provenance is the deterministic
            # fallback's own label means the model was invoked and fell back --
            # it did not resolve anything, whatever the rules-only pass says.
            if model_dx.classifier_provenance == "rules:fallback-v1":
                model_still_uncertain += 1
                continue
            model_ran = True
            if not _is_uncertain(model_dx):
                model_resolved += 1
                if model_dx.failure_class is truth[item_id].true_class:
                    model_correct += 1
            else:
                model_still_uncertain += 1

    return AmbiguityReport(
        total_items=len(fixture.items),
        uncertain_items=len(uncertain_ids),
        uncertain_value=round(uncertain_value, 2),
        unknown_items=unknown_count,
        low_confidence_items=low_confidence_count,
        addressable_recoverable_items=len(addressable_items),
        addressable_recoverable_value=round(addressable_value, 2),
        model_ran=model_ran,
        model_resolved_items=model_resolved,
        model_correctly_classified_items=model_correct,
        model_still_uncertain_items=model_still_uncertain,
    )


def to_dict(report: AmbiguityReport) -> dict:
    return {
        "total_items": report.total_items,
        "uncertain_items": report.uncertain_items,
        "uncertain_value": report.uncertain_value,
        "unknown_items": report.unknown_items,
        "low_confidence_items": report.low_confidence_items,
        "addressable_recoverable_items": report.addressable_recoverable_items,
        "addressable_recoverable_value": report.addressable_recoverable_value,
        "model_ran": report.model_ran,
        "model_resolved_items": report.model_resolved_items,
        "model_correctly_classified_items": report.model_correctly_classified_items,
        "model_still_uncertain_items": report.model_still_uncertain_items,
        "note": (
            "addressable_recoverable_value is a CEILING computed from the "
            "simulator's ground truth, not a delivered result -- it is the "
            "most a perfect classifier could be worth on this fixture's "
            "ambiguous items, before EV, budget or policy touch it. "
            "model_* fields are the measured result and are only nonzero if "
            "a live model classifier actually ran and produced output that "
            "was not itself a fallback."
        ),
    }
