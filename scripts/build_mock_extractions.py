"""Generate ``data/mock_extractions.json`` (the offline LLM mock fixture).

The mock adapter is a deterministic input->output map (REFACTOR_SPEC §10): a
known message yields a plausible ``ExtractedFeatures``. We author the curated
features per lead_id here, then key the fixture by the SAME normalized+redacted
message the runtime adapter will look up -- so e.g. "Sono Mario, ..." is keyed by
its redacted form "sono [NAME], ...". Re-run after editing the curated features:

    py -3 scripts/build_mock_extractions.py
"""

from __future__ import annotations

import json
from pathlib import Path

from src.extraction.llm import _normalize_message
from src.privacy import redact_message

_REPO = Path(__file__).resolve().parent.parent

# Curated "LLM outputs" per lead_id (the demo dataset). Leads invalidated by the
# structural gate (bogus phone / disposable email) never reach extraction, so
# they need no fixture entry.
FEATURES: dict[str, dict] = {
    "LEAD-0001": dict(budget_value_eur=35000, budget_present=True, vehicle_model_mentioned="SUV ibrido", vehicle_specificity="generic", trade_in_present=True, trade_in_vehicle="VW Golf 2018", urgency_signals=["test drive sabato mattina"], intent_strength="high", availability_mentioned=True, sentiment="positive", missing_critical_fields=[], looks_invalid=False, extraction_confidence=0.9, rationale_signals="budget chiaro, permuta Golf, disponibilità sabato, interesse al finanziamento"),
    "LEAD-0002": dict(budget_value_eur=30000, budget_present=True, vehicle_model_mentioned="Volkswagen T-Roc", vehicle_specificity="specific", trade_in_present=True, trade_in_vehicle="usato da permutare", urgency_signals=["con urgenza", "domani pomeriggio"], intent_strength="high", availability_mentioned=True, sentiment="positive", missing_critical_fields=[], looks_invalid=False, extraction_confidence=0.92, rationale_signals="urgenza esplicita, modello preciso, permuta, disponibilità domani"),
    "LEAD-0003": dict(budget_value_eur=45000, budget_present=True, vehicle_model_mentioned="Tesla Model 3", vehicle_specificity="specific", trade_in_present=True, trade_in_vehicle="auto attuale", urgency_signals=["pronto subito", "questo weekend"], intent_strength="high", availability_mentioned=True, sentiment="positive", missing_critical_fields=[], looks_invalid=False, extraction_confidence=0.93, rationale_signals="intento alto, budget elevato, permuta, finanziamento, disponibilità weekend"),
    "LEAD-0004": dict(budget_value_eur=28000, budget_present=True, vehicle_model_mentioned="Ford Puma", vehicle_specificity="specific", trade_in_present=True, trade_in_vehicle="auto attuale", urgency_signals=["con urgenza", "venerdì"], intent_strength="high", availability_mentioned=True, sentiment="positive", missing_critical_fields=[], looks_invalid=False, extraction_confidence=0.9, rationale_signals="urgenza, permuta, finanziamento, disponibilità venerdì"),
    "LEAD-0010": dict(budget_value_eur=None, budget_present=False, vehicle_model_mentioned="Renault Captur", vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None, urgency_signals=[], intent_strength="medium", availability_mentioned=False, sentiment="neutral", missing_critical_fields=["budget", "timeline_acquisto"], looks_invalid=False, extraction_confidence=0.85, rationale_signals="interesse su modello preciso, mancano budget e tempistiche"),
    "LEAD-0011": dict(budget_value_eur=18000, budget_present=True, vehicle_model_mentioned="Fiat 500", vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None, urgency_signals=[], intent_strength="medium", availability_mentioned=False, sentiment="neutral", missing_critical_fields=["timeline_acquisto"], looks_invalid=False, extraction_confidence=0.85, rationale_signals="budget indicato, richiesta informazioni"),
    "LEAD-0012": dict(budget_value_eur=None, budget_present=False, vehicle_model_mentioned="Kia Sportage", vehicle_specificity="specific", trade_in_present=True, trade_in_vehicle="vecchio diesel", urgency_signals=[], intent_strength="medium", availability_mentioned=False, sentiment="neutral", missing_critical_fields=["budget", "timeline_acquisto"], looks_invalid=False, extraction_confidence=0.85, rationale_signals="permuta presente, mancano budget e tempistiche"),
    "LEAD-0013": dict(budget_value_eur=None, budget_present=False, vehicle_model_mentioned="Peugeot 208", vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None, urgency_signals=[], intent_strength="low", availability_mentioned=False, sentiment="neutral", missing_critical_fields=["budget", "timeline_acquisto"], looks_invalid=False, extraction_confidence=0.8, rationale_signals="richiesta preventivo, segnali di intento deboli"),
    "LEAD-0014": dict(budget_value_eur=33000, budget_present=True, vehicle_model_mentioned="Hyundai Tucson", vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None, urgency_signals=[], intent_strength="medium", availability_mentioned=False, sentiment="neutral", missing_critical_fields=["timeline_acquisto"], looks_invalid=False, extraction_confidence=0.85, rationale_signals="budget indicato, richiesta promozioni"),
    "LEAD-0020": dict(budget_value_eur=None, budget_present=False, vehicle_model_mentioned=None, vehicle_specificity="none", trade_in_present=False, trade_in_vehicle=None, urgency_signals=[], intent_strength="low", availability_mentioned=False, sentiment="neutral", missing_critical_fields=["budget", "modello", "timeline_acquisto"], looks_invalid=False, extraction_confidence=0.5, rationale_signals="messaggio molto generico, scarsi segnali"),
    "LEAD-0021": dict(budget_value_eur=None, budget_present=False, vehicle_model_mentioned=None, vehicle_specificity="none", trade_in_present=False, trade_in_vehicle=None, urgency_signals=[], intent_strength="low", availability_mentioned=False, sentiment="neutral", missing_critical_fields=["budget", "modello", "timeline_acquisto"], looks_invalid=False, extraction_confidence=0.55, rationale_signals="lead in fase esplorativa"),
    "LEAD-0022": dict(budget_value_eur=None, budget_present=False, vehicle_model_mentioned=None, vehicle_specificity="none", trade_in_present=False, trade_in_vehicle=None, urgency_signals=[], intent_strength="low", availability_mentioned=False, sentiment="neutral", missing_critical_fields=["budget", "modello", "timeline_acquisto"], looks_invalid=False, extraction_confidence=0.5, rationale_signals="solo saluto, nessun segnale di intento"),
    "LEAD-0032": dict(budget_value_eur=None, budget_present=False, vehicle_model_mentioned=None, vehicle_specificity="none", trade_in_present=False, trade_in_vehicle=None, urgency_signals=[], intent_strength="low", availability_mentioned=False, sentiment="neutral", missing_critical_fields=[], looks_invalid=True, extraction_confidence=0.8, rationale_signals="messaggio incomprensibile (gibberish)"),
    "LEAD-0040": dict(budget_value_eur=20000, budget_present=True, vehicle_model_mentioned="Toyota Yaris", vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None, urgency_signals=["prossima settimana"], intent_strength="medium", availability_mentioned=True, sentiment="positive", missing_critical_fields=[], looks_invalid=False, extraction_confidence=0.88, rationale_signals="modello preciso, budget, disponibilità prossima settimana"),
    "LEAD-0041": dict(budget_value_eur=28000, budget_present=True, vehicle_model_mentioned="Volkswagen Golf", vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None, urgency_signals=[], intent_strength="medium", availability_mentioned=False, sentiment="neutral", missing_critical_fields=["timeline_acquisto"], looks_invalid=False, extraction_confidence=0.85, rationale_signals="budget indicato, richiesta preventivo"),
    "LEAD-0042": dict(budget_value_eur=32000, budget_present=True, vehicle_model_mentioned="Jeep Renegade", vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None, urgency_signals=["sabato"], intent_strength="high", availability_mentioned=True, sentiment="positive", missing_critical_fields=[], looks_invalid=False, extraction_confidence=0.9, rationale_signals="budget, disponibilità sabato, modello preciso"),
    "LEAD-0050": dict(budget_value_eur=35000, budget_present=True, vehicle_model_mentioned="SUV ibrido", vehicle_specificity="generic", trade_in_present=True, trade_in_vehicle="Golf", urgency_signals=["sabato"], intent_strength="high", availability_mentioned=True, sentiment="positive", missing_critical_fields=[], looks_invalid=False, extraction_confidence=0.9, rationale_signals="conferma interesse, permuta, disponibilità sabato"),
    "LEAD-0051": dict(budget_value_eur=None, budget_present=False, vehicle_model_mentioned="Fiat 500", vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None, urgency_signals=[], intent_strength="medium", availability_mentioned=False, sentiment="neutral", missing_critical_fields=["budget", "timeline_acquisto"], looks_invalid=False, extraction_confidence=0.8, rationale_signals="ripresa contatto su modello già richiesto"),
    "LEAD-0060": dict(budget_value_eur=38000, budget_present=True, vehicle_model_mentioned="SUV ibrido", vehicle_specificity="generic", trade_in_present=True, trade_in_vehicle="auto attuale", urgency_signals=["sabato"], intent_strength="high", availability_mentioned=True, sentiment="positive", missing_critical_fields=[], looks_invalid=False, extraction_confidence=0.9, rationale_signals="cliente di ritorno, secondo acquisto, permuta, disponibilità sabato"),
    "LEAD-0061": dict(budget_value_eur=31000, budget_present=True, vehicle_model_mentioned="Volkswagen T-Roc", vehicle_specificity="specific", trade_in_present=True, trade_in_vehicle="usato da permutare", urgency_signals=[], intent_strength="high", availability_mentioned=False, sentiment="positive", missing_critical_fields=[], looks_invalid=False, extraction_confidence=0.88, rationale_signals="cliente esistente, modello preciso, permuta"),
    "LEAD-0062": dict(budget_value_eur=33000, budget_present=True, vehicle_model_mentioned="Jeep Renegade", vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None, urgency_signals=["test drive"], intent_strength="high", availability_mentioned=True, sentiment="positive", missing_critical_fields=[], looks_invalid=False, extraction_confidence=0.88, rationale_signals="cliente di ritorno, disponibilità per test drive"),
    "LEAD-0070": dict(budget_value_eur=27000, budget_present=True, vehicle_model_mentioned="Nissan Qashqai", vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None, urgency_signals=[], intent_strength="medium", availability_mentioned=False, sentiment="neutral", missing_critical_fields=["timeline_acquisto"], looks_invalid=False, extraction_confidence=0.84, rationale_signals="richiesta disponibilità e prezzo, budget indicato"),
}

# Curated "LLM outputs" for the user's REPLIES to a recover-info prompt. The agent
# re_extracts the reply (PII-redacted) and the planner merges + re-scores it
# (§7.2), so a reply that supplies the missing fields lifts the lead's band. Keyed
# by the same normalized+redacted reply text. Without a fixture an unknown reply
# yields a low-confidence default (no false "complete"): the agent then hands the
# enriched lead to a human instead of fabricating understanding.
REPLIES: dict[str, dict] = {
    # Full recovery (budget + timeline): completes the lead, high intent -> the
    # re-score typically lifts it to hot and the agent attempts the booking.
    "Il mio budget è 25000 euro, vorrei comprare entro un mese.": dict(budget_value_eur=25000, budget_present=True, vehicle_model_mentioned=None, vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None, urgency_signals=["entro un mese"], intent_strength="high", availability_mentioned=False, sentiment="neutral", missing_critical_fields=[], looks_invalid=False, extraction_confidence=0.9, rationale_signals="budget e tempistica forniti, intento alto"),
    # Partial recovery (only budget): one field still missing -> the agent keeps
    # chasing the remaining info (bounded by the message budget).
    "il budget è circa 25000 euro.": dict(budget_value_eur=25000, budget_present=True, vehicle_model_mentioned=None, vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None, urgency_signals=[], intent_strength="medium", availability_mentioned=False, sentiment="neutral", missing_critical_fields=["timeline_acquisto"], looks_invalid=False, extraction_confidence=0.85, rationale_signals="budget fornito, manca la tempistica"),
}


# Curated high-value but INCOMPLETE base for the agent recovery tests: strong
# intent/model/availability signals (so score >= warm_high and the agent triggers)
# with BOTH budget and timeline still missing -- a partial reply then resolves one
# field while the agent keeps chasing the other. Keyed by its own text (not a demo
# lead). Its structured score depends on the caller's lead fields.
TEST_BASES: dict[str, dict] = {
    "Sono molto interessato alla Renault Captur, vorrei vederla e provarla il prima possibile.": dict(
        budget_value_eur=None, budget_present=False, vehicle_model_mentioned="Renault Captur",
        vehicle_specificity="specific", trade_in_present=False, trade_in_vehicle=None,
        urgency_signals=["il prima possibile"], intent_strength="high", availability_mentioned=True,
        sentiment="positive", missing_critical_fields=["budget", "timeline_acquisto"],
        looks_invalid=False, extraction_confidence=0.9,
        rationale_signals="intento alto e disponibilità dichiarata, mancano budget e tempistica",
    ),
}


def main() -> None:
    leads = json.loads((_REPO / "data" / "leads_mock.json").read_text(encoding="utf-8"))
    by_id = {lead["lead_id"]: lead for lead in leads}

    by_message: dict[str, dict] = {}
    for lead_id, feats in FEATURES.items():
        lead = by_id.get(lead_id)
        if lead is None:
            continue
        key = _normalize_message(redact_message(lead.get("message")))
        by_message[key] = feats

    # Recovery replies + test bases are keyed by their own (normalized+redacted) text.
    for message, feats in {**REPLIES, **TEST_BASES}.items():
        by_message[_normalize_message(redact_message(message))] = feats

    out = {
        "_comment": "Offline LLM mock fixture (REFACTOR_SPEC §10). Keyed by the "
        "normalized+redacted message. Generated by scripts/build_mock_extractions.py.",
        "by_message": by_message,
    }
    dest = _REPO / "data" / "mock_extractions.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(by_message)} fixtures -> {dest}")


if __name__ == "__main__":
    main()
