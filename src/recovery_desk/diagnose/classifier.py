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


class ModelClassifier:
    """Claude reads the unbounded text; the enum bounds what it can say.

    Three properties make this safe to put in a money pipeline: output is
    coerced into ``FailureClass`` or rejected, calls are cached by failure
    signature so a 1,000-item batch costs a few dozen calls, and any error at
    all falls through to the deterministic classifier rather than failing the
    batch.
    """

    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        cache_path: Path | None = None,
        fallback: Classifier | None = None,
    ) -> None:
        self.model = model
        self.provenance = "model:" + model
        self.cache_path = cache_path or Path(".cache/diagnosis.json")
        self.fallback = fallback or DeterministicClassifier()
        self._cache: dict[str, dict] = self._load_cache()
        self._client = None
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
    def available(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def classify(self, item: AtRiskItem) -> Diagnosis:
        key = signature(item.raw_gateway_context)
        cached = self._cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return self._to_diagnosis(item, cached)

        payload = self._call_model(item.raw_gateway_context)
        if payload is None:
            self.fallbacks_used += 1
            return self.fallback.classify(item)

        self._cache[key] = payload
        return self._to_diagnosis(item, payload)

    def _to_diagnosis(self, item: AtRiskItem, payload: dict) -> Diagnosis:
        failure_class = FailureClass(payload["failure_class"])
        return Diagnosis(
            item_id=item.id,
            failure_class=failure_class,
            confidence=float(payload["confidence"]),
            evidence=payload["evidence"],
            recovery_prior=priors.headline_prior(failure_class),
            classifier_provenance=self.provenance,
        )

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
            text = response.content[0].text.strip()
            text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
            payload = json.loads(text)
            # The enum is the bound. Anything outside it raises here and is counted.
            FailureClass(payload["failure_class"])
            self.calls_made += 1
            return payload
        except (ValueError, KeyError, TypeError, IndexError):
            self.rejected_outputs += 1
            return None
        except Exception:
            # Transport, auth, rate limit: the batch continues on rules.
            return None

    def stats(self) -> dict[str, int]:
        return {
            "calls_made": self.calls_made,
            "cache_hits": self.cache_hits,
            "rejected_outputs": self.rejected_outputs,
            "fallbacks_used": self.fallbacks_used,
        }


def diagnose(items: Sequence[AtRiskItem], classifier: Classifier) -> list[Diagnosis]:
    """Stage 2 boundary."""
    return [classifier.classify(item) for item in items]
