"""Gated, PII-safe semantic extraction (the single hot-path LLM call).

Order of operations:
  1. GATE: never spend the call on an invalid lead or an empty/trivial message.
  2. REDACT PII before the message reaches the adapter, then assert no raw PII.
  3. ONE adapter call. On any failure/timeout, degrade to a structured-only
     default flagged ``fallback`` (score then relies on deterministic features
     only, ``low_confidence``) so the SLA never depends on the LLM (§8).

There is intentionally NO keyword/rules "NLP" fallback: semantic understanding
is the LLM's job (the user's hard requirement). Offline, the adapter's mock is a
deterministic fixture; when even that has no entry it returns low confidence.
"""

from __future__ import annotations

from src.extraction.llm import LLMAdapter, LLMError
from src.models.lead import ExtractedFeatures, Lead
from src.models.scoring import ValidityResult
from src.privacy import assert_no_raw_pii, redact_message, safe_fields_for_llm

# A message shorter than this (after stripping) carries no extractable intent.
_MIN_MESSAGE_CHARS = 3


def _is_trivial(message: str | None) -> bool:
    if not message:
        return True
    return len(message.strip()) < _MIN_MESSAGE_CHARS


def extract_features(
    lead: Lead,
    validity: ValidityResult,
    adapter: LLMAdapter,
) -> ExtractedFeatures:
    """Extract semantic features for a lead, gated and PII-safe.

    Returns ``ExtractedFeatures`` whose ``extraction_source`` is one of
    {"skipped", "mock", "llm", "fallback"}.
    """
    if not validity.is_valid or _is_trivial(lead.message):
        return ExtractedFeatures(extraction_source="skipped")

    redacted = redact_message(lead.message)
    assert_no_raw_pii(redacted)
    context = safe_fields_for_llm(lead)

    try:
        return adapter.extract(redacted, context)
    except LLMError:
        return _fallback()
    except Exception:  # noqa: BLE001 - the score path must never block on the LLM
        return _fallback()


def _fallback() -> ExtractedFeatures:
    """Structured-only default after an LLM failure/timeout (low confidence)."""
    return ExtractedFeatures(
        extraction_source="fallback",
        extraction_confidence=0.0,
        rationale_signals="(fallback) LLM non disponibile: scoring sui soli campi strutturali",
    )
