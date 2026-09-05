"""Smoke tests for the bounded AI adapter.

Not exhaustive by design. These pin the five properties that matter for a
money pipeline: malformed output is rejected rather than trusted or crashing,
a corrupted cache is treated the same way a corrupted fresh response is, the
deterministic fallback always produces a usable diagnosis, and the classifier
never claims a class outside the enum.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recovery_desk.diagnose.classifier import (
    DeterministicClassifier,
    ModelClassifier,
    validate_payload,
)
from recovery_desk.models import ActionType, AtRiskItem, FailureClass
from datetime import datetime, timezone


def _item(raw: str = "gateway error 504 upstream deadline exceeded") -> AtRiskItem:
    return AtRiskItem(
        id="itm_test",
        type=__import__("recovery_desk.models", fromlist=["ItemType"]).ItemType.PAYMENT_FAILURE,
        amount=1000.0,
        currency="INR",
        merchant_id="mrc_test",
        customer_id="cust_test",
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source="payments_stream",
        raw_gateway_context=raw,
    )


def test_malformed_ai_output_is_rejected_not_trusted():
    """Every shape of bad output validate_payload sees returns None, not a crash."""
    bad_payloads = [
        {"failure_class": "not_a_real_class", "confidence": 0.8, "evidence": "x"},
        {"failure_class": "unknown", "confidence": "high", "evidence": "x"},
        {"failure_class": "unknown", "confidence": 1.5, "evidence": "x"},
        {"failure_class": "unknown", "confidence": -0.1, "evidence": "x"},
        {"failure_class": "unknown", "confidence": 0.5, "evidence": ""},
        {"failure_class": "unknown", "confidence": 0.5},  # missing evidence
        {"confidence": 0.5, "evidence": "x"},  # missing failure_class
        "not a dict at all",
        None,
        42,
    ]
    for payload in bad_payloads:
        assert validate_payload(payload) is None, payload


def test_valid_payload_is_normalised_and_bounded():
    payload = validate_payload(
        {
            "failure_class": "wrong_pin_or_attempts_exceeded",
            "confidence": 0.8,
            "evidence": "one two three four five six seven eight nine ten eleven twelve thirteen",
        }
    )
    assert payload is not None
    assert payload["failure_class"] == "wrong_pin_or_attempts_exceeded"
    assert len(payload["evidence"].split()) <= 13  # 12 words + the ellipsis token


def test_corrupted_cache_entry_falls_back_instead_of_crashing():
    """A hand-edited or corrupted cache file must never crash the batch."""
    classifier = ModelClassifier(cache_path=Path("/nonexistent/does-not-matter.json"))
    classifier._cache["deadbeefdeadbeef"] = {
        "failure_class": "totally_invalid",
        "confidence": "not a number",
    }
    # Force the signature to collide with our poisoned cache entry.
    import recovery_desk.diagnose.classifier as classifier_module

    original_signature = classifier_module.signature
    classifier_module.signature = lambda raw: "deadbeefdeadbeef"
    try:
        diagnosis = classifier.classify(_item())
    finally:
        classifier_module.signature = original_signature

    # No API key in this environment, so it falls through to rules -- and
    # critically, it returns a valid Diagnosis rather than raising.
    assert diagnosis.failure_class in FailureClass
    assert classifier.rejected_outputs >= 1
    assert classifier.fallbacks_used >= 1


def test_fallback_works_when_no_api_key_present():
    """With no ANTHROPIC_API_KEY, every classification safely falls to rules."""
    classifier = ModelClassifier(cache_path=Path("/nonexistent/does-not-matter2.json"))
    assert classifier.available is False
    diagnosis = classifier.classify(_item())
    assert diagnosis.failure_class in FailureClass
    # The diagnosis honestly attributes itself to whichever component actually
    # produced it -- here, the deterministic fallback, not the model.
    assert diagnosis.classifier_provenance == "rules:fallback-v1"
    stats = classifier.stats()
    assert stats["calls_made"] == 0
    assert stats["fallbacks_used"] == 1
    assert stats["had_api_key"] is False


def test_classifier_never_produces_a_class_outside_the_closed_enum():
    """The FailureClass enum is the bound; nothing upstream can widen it."""
    rules = DeterministicClassifier()
    for text in (
        "gateway error 504 upstream deadline exceeded",
        "completely novel gateway text never seen before xyzzy",
        "",
    ):
        diagnosis = rules.classify(_item(text))
        assert diagnosis.failure_class in FailureClass


def test_deterministic_action_coercion_rejects_unknown_action_names():
    """The closed action enumeration: any name outside it resolves to None."""
    from recovery_desk.decide.policy import coerce_proposed_action

    assert coerce_proposed_action("retry_now") is ActionType.RETRY_NOW
    assert coerce_proposed_action("wire_transfer_immediately") is None
    assert coerce_proposed_action("") is None
