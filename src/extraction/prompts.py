"""Centralized prompt and strict JSON schema for the semantic extractor.

The extractor is the system's single LLM use in the hot path. It reads the
(already PII-redacted) Italian ``message`` and returns the ``ExtractedFeatures``
of REFACTOR_SPEC §5.2. It NEVER assigns a score; the extracted signals feed the
deterministic ``build_feature_vector`` downstream.

These constants are consumed only by the OpenAI path of the adapter. The mock
path is a deterministic fixture map and does not use them.
"""

from __future__ import annotations

EXTRACTOR_SYSTEM_PROMPT = """\
You are a precise information-extraction engine for an Italian automotive lead.

You receive a single free-text message written in Italian by a prospective car
buyer, plus a few non-PII structured fields for context. Personally identifiable
information (phone, email, name) has already been redacted to tokens such as
[PHONE], [EMAIL], [NAME]; treat them as opaque and never infer intent from them.

Extract ONLY these purchase-intent / quality signals and return them as JSON:

- budget_value_eur (number|null): stated budget in euros as a plain number
  ("35k" -> 35000, "35.000" -> 35000, "35 mila" -> 35000). null if none.
- budget_present (boolean): true iff an explicit budget is stated.
- vehicle_model_mentioned (string|null): the vehicle the buyer refers to
  (verbatim-ish), or null.
- vehicle_specificity ("specific"|"generic"|"none"): "specific" if a precise
  model/trim is named (e.g. "Toyota C-HR"), "generic" if only a category
  ("un SUV", "una berlina"), "none" if no vehicle is referenced.
- trade_in_present (boolean): true iff the buyer mentions trading in a vehicle.
- trade_in_vehicle (string|null): the trade-in vehicle described, or null.
- urgency_signals (array of short strings): verbatim cues about timing
  ("disponibile sabato", "ho urgenza"); empty if none.
- intent_strength ("high"|"medium"|"low"): overall purchase intent. "high" =
  ready to act now/this week; "medium" = actively evaluating; "low" = browsing.
- availability_mentioned (boolean): true iff the buyer offers availability for a
  call or test drive.
- sentiment ("positive"|"neutral"|"negative"): tone toward the purchase.
- missing_critical_fields (array of strings): critical info still missing to
  qualify (e.g. "timeline_acquisto", "budget", "modello"); empty if none.
- looks_invalid (boolean): true ONLY if the message itself is clearly spam,
  gibberish, a test, or evidently out of scope (NOT merely incomplete).
- extraction_confidence (number 0..1): your confidence in this extraction.
- rationale_signals (string): a short Italian phrase summarizing the strongest
  signals; it will be shown to a call-center operator.

Rules:
- Base every field strictly on the message and context. Do not invent.
- Output ONLY a JSON object matching the schema, no commentary, no markdown.\
"""

EXTRACTION_JSON_SCHEMA: dict = {
    "name": "extracted_features",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "budget_value_eur": {"type": ["number", "null"]},
            "budget_present": {"type": "boolean"},
            "vehicle_model_mentioned": {"type": ["string", "null"]},
            "vehicle_specificity": {
                "type": "string",
                "enum": ["specific", "generic", "none"],
            },
            "trade_in_present": {"type": "boolean"},
            "trade_in_vehicle": {"type": ["string", "null"]},
            "urgency_signals": {"type": "array", "items": {"type": "string"}},
            "intent_strength": {"type": "string", "enum": ["high", "medium", "low"]},
            "availability_mentioned": {"type": "boolean"},
            "sentiment": {
                "type": "string",
                "enum": ["positive", "neutral", "negative"],
            },
            "missing_critical_fields": {
                "type": "array",
                "items": {"type": "string"},
            },
            "looks_invalid": {"type": "boolean"},
            "extraction_confidence": {"type": "number"},
            "rationale_signals": {"type": "string"},
        },
        "required": [
            "budget_value_eur",
            "budget_present",
            "vehicle_model_mentioned",
            "vehicle_specificity",
            "trade_in_present",
            "trade_in_vehicle",
            "urgency_signals",
            "intent_strength",
            "availability_mentioned",
            "sentiment",
            "missing_critical_fields",
            "looks_invalid",
            "extraction_confidence",
            "rationale_signals",
        ],
    },
}


def build_extraction_messages(
    redacted_message: str, context: dict | None = None
) -> list[dict]:
    """Build the chat messages for the extraction call (PII already redacted)."""
    user = redacted_message
    if context:
        # Append non-PII structured context (channel/campaign/vehicle/city...).
        ctx = ", ".join(f"{k}={v}" for k, v in context.items())
        user = f"{redacted_message}\n\n[context: {ctx}]"
    return [
        {"role": "system", "content": EXTRACTOR_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
