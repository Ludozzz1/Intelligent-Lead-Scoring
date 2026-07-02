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
- vehicle_model_mentioned (string|null): the specific vehicle of interest. The
  context's `vehicle_interest` is AUTHORITATIVE: if it names a precise model (e.g.
  "Toyota C-HR"), use it even when the message only says a category ("un SUV").
- vehicle_specificity ("specific"|"generic"|"none"): "specific" if a precise
  model/trim is identifiable from EITHER the message OR the context `vehicle_interest`
  (e.g. "Toyota C-HR"); "generic" only if the vehicle is just a category ("un SUV",
  "una berlina") AND no specific `vehicle_interest` is given; "none" if no vehicle.
- trade_in_present (boolean): true iff the buyer mentions trading in a vehicle.
- trade_in_vehicle (string|null): the trade-in vehicle described, or null.
- urgency_signals (array of short strings): verbatim cues about timing
  ("disponibile sabato", "ho urgenza"); empty if none.
- intent_strength ("high"|"medium"|"low"): overall purchase intent. "high" =
  ready to act now/this week; "medium" = actively evaluating; "low" = browsing.
- availability_mentioned (boolean): true iff the buyer offers availability for a
  call or test drive.
- sentiment ("positive"|"neutral"|"negative"): tone toward the purchase.
- missing_critical_fields (array): critical info still missing to QUALIFY the lead.
  Use ONLY these exact keys, never invent others: "budget", "timeline_acquisto",
  "modello". List "modello" ONLY if no specific model is identifiable from the message
  OR the context `vehicle_interest`. Empty if nothing critical is missing.
- looks_invalid (boolean): true ONLY if the message itself is clearly spam,
  gibberish, a test, or evidently out of scope (NOT merely incomplete).
- extraction_confidence (number 0..1): your confidence in this extraction.
- rationale_signals (string): a short Italian phrase summarizing the strongest
  signals; it will be shown to a call-center operator.

Rules:
- Base every field strictly on the message and context. Do not invent.
- The context's `vehicle_interest` is the authoritative vehicle: when it names a
  specific model, the lead's vehicle IS that model even if the message is vaguer, so
  vehicle_specificity is "specific" and "modello" is NOT missing.
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
                "items": {
                    "type": "string",
                    "enum": ["budget", "timeline_acquisto", "modello"],
                },
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
    redacted_message: str,
    context: dict | None = None,
    reply_context: dict | None = None,
) -> list[dict]:
    """Build the chat messages for the extraction call (PII already redacted).

    ``reply_context`` (optional) REFRAMES the extraction: it marks the message as the
    customer's reply to the agent's question for specific missing fields, so a short
    answer (e.g. "entro due settimane") is read as the answer to THAT question instead
    of being extracted in a vacuum. Its presence is the "there is context" switch that
    builds the prompt differently. Consumed only by the OpenAI path (the mock is a
    fixture map and ignores it).
    """
    user = redacted_message
    if reply_context:
        vehicle = reply_context.get("vehicle") or "il veicolo d'interesse"
        fields = reply_context.get("fields") or []
        fields_str = ", ".join(fields) if fields else "le informazioni mancanti"
        frame = (
            "The message below is the customer's REPLY to the agent's question asking "
            f"for these still-missing field(s): {fields_str} (vehicle: {vehicle}). Read "
            "it as the ANSWER to that question and extract accordingly: a short reply "
            'like "entro due settimane" supplies timeline_acquisto, "circa 20 mila" '
            "supplies budget. List a field in missing_critical_fields ONLY if this "
            "reply still does not provide it."
        )
        user = f"[{frame}]\n\nCustomer reply: {redacted_message}"
    if context:
        # Append non-PII structured context (channel/campaign/vehicle/city...).
        ctx = ", ".join(f"{k}={v}" for k, v in context.items())
        user = f"{user}\n\n[context: {ctx}]"
    return [
        {"role": "system", "content": EXTRACTOR_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
