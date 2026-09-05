"""Stage 2. The seam between deterministic logic and the model.

Both arms return the same ``Diagnosis``, so the pipeline cannot tell them apart
and the harness can swap one for the other to measure what the model is worth.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Protocol, Sequence

from ..models import AtRiskItem, Diagnosis, FailureClass
from . import priors
from .fallback import classify_text

SYSTEM_PROMPT = (
    "You classify payment gateway failure messages for an Indian payments desk.\n"
    "Reply with JSON only, no prose, in exactly this shape:\n"
    '{"failure_class": "<one of: %s>", "confidence": <number 0-1>, '
    '"evidence": "<at most 12 words quoting the text>"}\n'
    "Use unknown when the text does not clearly indicate a class. "
    "Never invent a class outside the list."
)

#: Hard bound on evidence length, enforced regardless of what the model claims
#: to have done. The prompt asks for "at most 12 words"; this is what actually
#: guarantees it, so a verbose or adversarial response can't inflate the audit
#: log or the UI with unbounded text.
MAX_EVIDENCE_WORDS = 12


def validate_payload(payload: object) -> dict | None:
    """The one gate every model output passes through, cache hit or fresh call.

    Returns a normalised, bounded dict, or None if anything about the payload is
    wrong -- wrong shape, a class outside the closed enum, a confidence that
    isn't a real number in [0, 1], or an evidence field that isn't text. None
    means "reject and fall back"; it never means "raise and crash the batch."

    This exists as one function, not two, because a cache entry is just a model
    response from an earlier run -- if unvalidated output could reach a
    Diagnosis by one path, a corrupted or hand-edited cache file would crash a
    batch that never touched the network this run.
    """
    if not isinstance(payload, dict):
        return None
    try:
        failure_class = FailureClass(payload["failure_class"])
    except (KeyError, ValueError, TypeError):
        return None

    try:
        confidence = float(payload["confidence"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0.0 <= confidence <= 1.0):
        return None

    evidence = payload.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return None
    words = evidence.split()
    if len(words) > MAX_EVIDENCE_WORDS:
        evidence = " ".join(words[:MAX_EVIDENCE_WORDS]) + "…"

    return {
        "failure_class": failure_class.value,
        "confidence": confidence,
        "evidence": evidence,
    }


class Classifier(Protocol):
    provenance: str

    def classify(self, item: AtRiskItem) -> Diagnosis: ...


class DeterministicClassifier:
    """Rules only. No network, no sampler, no variance between runs."""

    provenance = "rules:fallback-v1"

    def classify(self, item: AtRiskItem) -> Diagnosis:
        failure_class, confidence, evidence = classify_text(item.raw_gateway_context)
        return Diagnosis(
            item_id=item.id,
            failure_class=failure_class,
            confidence=confidence,
            evidence=evidence,
            recovery_prior=priors.headline_prior(failure_class),
            classifier_provenance=self.provenance,
        )


def signature(raw: str) -> str:
    """Collapse a gateway string to its shape, so lookalikes share one model call.

    Digits and identifiers are the parts that vary between two instances of the
    same failure; stripping them is what makes caching effective at batch scale.
    """
    shape = re.sub(r"\d+", "#", raw.lower())
    shape = re.sub(r"\s+", " ", shape).strip()
    return hashlib.sha256(shape.encode()).hexdigest()[:16]


class _CachedModelClassifier:
    """Shared safety gate for every model-backed classifier.

    Provider adapters (Anthropic, Gemini, ...) differ only in how they make one
    network call and unwrap its response; everything that makes a model safe to
    put in a money pipeline lives here exactly once, so a second provider cannot
    quietly bypass it. Every payload -- fresh or cached -- passes through
    ``validate_payload`` before it can become a ``Diagnosis``: output is coerced
    into the closed ``FailureClass`` enum or rejected, and any error at all
    (network, auth, malformed output) falls through to the deterministic
    classifier rather than failing the batch. Subclasses implement only
    ``available`` and ``_call_model``.
    """

    #: Set by each subclass; kept here only so callers can annotate on the base.
    model: str
    provenance: str

    def __init__(
        self,
        cache_path: Path,
        fallback: Classifier | None = None,
    ) -> None:
        self.cache_path = cache_path
        self.fallback = fallback or DeterministicClassifier()
        self._cache: dict[str, dict] = self._load_cache()
        self.calls_made = 0
        self.cache_hits = 0
        self.rejected_outputs = 0
        self.fallbacks_used = 0

    # -- cache ------------------------------------------------------------

    def _load_cache(self) -> dict[str, dict]:
        if self.cache_path.exists():
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        return {}

    def flush(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache, indent=1), encoding="utf-8")

    # -- classification ---------------------------------------------------

    @property
    def available(self) -> bool:  # pragma: no cover - overridden by subclasses
        raise NotImplementedError

    def _call_model(self, raw: str) -> dict | None:  # pragma: no cover
        raise NotImplementedError

    def classify(self, item: AtRiskItem) -> Diagnosis:
        key = signature(item.raw_gateway_context)
        cached = self._cache.get(key)
        if cached is not None:
            validated = validate_payload(cached)
            if validated is not None:
                self.cache_hits += 1
                return self._to_diagnosis(item, validated)
            # A corrupted cache entry is treated exactly like a corrupted fresh
            # response: rejected and counted, never trusted, never raised.
            self.rejected_outputs += 1

        payload = self._call_model(item.raw_gateway_context)
        if payload is None:
            self.fallbacks_used += 1
            return self.fallback.classify(item)

        self._cache[key] = payload
        return self._to_diagnosis(item, payload)

    def _to_diagnosis(self, item: AtRiskItem, payload: dict) -> Diagnosis:
        # payload is only ever a dict that has already passed validate_payload,
        # from either call site above -- this is not a second validation gate.
        failure_class = FailureClass(payload["failure_class"])
        return Diagnosis(
            item_id=item.id,
            failure_class=failure_class,
            confidence=payload["confidence"],
            evidence=payload["evidence"],
            recovery_prior=priors.headline_prior(failure_class),
            classifier_provenance=self.provenance,
        )

    def stats(self) -> dict[str, int | bool | str]:
        return {
            "model": self.model,
            "had_api_key": self.available,
            "calls_made": self.calls_made,
            "cache_hits": self.cache_hits,
            "rejected_outputs": self.rejected_outputs,
            "fallbacks_used": self.fallbacks_used,
        }


class ModelClassifier(_CachedModelClassifier):
    """Claude reads the unbounded text; the enum bounds what it can say.

    See ``_CachedModelClassifier`` for the three safety properties this and
    every other provider adapter share.
    """

    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        cache_path: Path | None = None,
        fallback: Classifier | None = None,
    ) -> None:
        self.model = model
        self.provenance = "model:" + model
        self._client = None
        super().__init__(cache_path or Path(".cache/diagnosis.json"), fallback)

    @property
    def available(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def _call_model(self, raw: str) -> dict | None:
        if not self.available:
            return None
        try:
            if self._client is None:
                import anthropic

                self._client = anthropic.Anthropic()
            allowed = ", ".join(c.value for c in FailureClass)
            response = self._client.messages.create(
                model=self.model,
                max_tokens=200,
                system=SYSTEM_PROMPT % allowed,
                messages=[{"role": "user", "content": raw}],
            )
        except Exception:
            # Transport, auth, rate limit: a call that never completed is not a
            # rejection, it is simply not a call. The batch falls back on rules.
            return None

        # The network round-trip happened; this counts as a call regardless of
        # what the model said, because "calls_made" measures API usage, not
        # output quality. Whether the output survives validation is separate.
        self.calls_made += 1
        try:
            text = response.content[0].text.strip()
            text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
            raw_payload = json.loads(text)
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            self.rejected_outputs += 1
            return None

        validated = validate_payload(raw_payload)
        if validated is None:
            # Well-formed JSON, wrong contents: an invented class, an
            # out-of-range confidence, empty evidence. Rejected all the same.
            self.rejected_outputs += 1
            return None
        return validated


class GeminiClassifier(_CachedModelClassifier):
    """The same seam, on Gemini instead of Claude.

    Exists for one reason: a contributor without Anthropic billing set up can
    still measure a real model arm, on Google AI Studio's free tier, through
    the identical safety gate in ``_CachedModelClassifier``. Nothing downstream
    -- the EV math, the allocator, the policy gate -- can tell which provider
    produced a ``Diagnosis``; both share the ``model:`` provenance prefix the
    UI uses to draw the AI boundary.
    """

    def __init__(
        self,
        model: str = "gemini-3.6-flash",
        cache_path: Path | None = None,
        fallback: Classifier | None = None,
    ) -> None:
        self.model = model
        self.provenance = "model:" + model
        self._client = None
        super().__init__(cache_path or Path(".cache/diagnosis_gemini.json"), fallback)

    @property
    def available(self) -> bool:
        return bool(
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        )

    def _call_model(self, raw: str) -> dict | None:
        if not self.available:
            return None
        try:
            if self._client is None:
                import google.generativeai as genai

                genai.configure(
                    api_key=os.environ.get("GEMINI_API_KEY")
                    or os.environ.get("GOOGLE_API_KEY")
                )
                allowed = ", ".join(c.value for c in FailureClass)
                self._client = genai.GenerativeModel(
                    self.model, system_instruction=SYSTEM_PROMPT % allowed
                )
            response = self._client.generate_content(raw)
        except Exception:
            # Same contract as the Anthropic adapter: transport, auth, quota,
            # a blocked-content response with no text -- any of it is simply
            # not a call, and the batch falls back to rules.
            return None

        self.calls_made += 1
        try:
            text = response.text.strip()
            text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
            raw_payload = json.loads(text)
        except Exception:
            # Broad on purpose: response.text itself raises for a response with
            # no text part (e.g. safety-filtered), which is a rejection here,
            # not a crash.
            self.rejected_outputs += 1
            return None

        validated = validate_payload(raw_payload)
        if validated is None:
            self.rejected_outputs += 1
            return None
        return validated


def diagnose(items: Sequence[AtRiskItem], classifier: Classifier) -> list[Diagnosis]:
    """Stage 2 boundary."""
    return [classifier.classify(item) for item in items]
