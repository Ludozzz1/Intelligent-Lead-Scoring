"""Deterministic motivation (REFACTOR_SPEC §5.5 — NO second LLM call).

The operator-facing rationale is built from the LLM extraction's
``rationale_signals`` plus the top per-feature score contributions. There is no
explainer LLM call: motivation is a deterministic function of already-computed
data, so the hot path stays at a single LLM call.
"""

from __future__ import annotations

from src.models.features import ScoreResult
from src.models.lead import ExtractedFeatures
from src.models.scoring import ValidityResult
from src.scoring.scorer import top_contributions

# Italian operator-facing labels for the §5.3 features.
_FEATURE_LABELS = {
    "intent_strength": "intento d'acquisto",
    "budget_present": "budget dichiarato",
    "reachability": "contatto raggiungibile",
    "vehicle_specificity": "modello specifico",
    "availability": "disponibilità dichiarata",
    "trade_in_present": "permuta presente",
    "geo_match": "area coperta",
    "recency": "lead recente",
    "sentiment": "sentiment positivo",
}

# Italian rendering of structural gate reasons.
_REASON_LABELS = {
    "phone_bogus": "telefono non plausibile",
    "email_disposable": "email usa-e-getta",
    "no_reachable_contact": "nessun contatto raggiungibile",
}


def build_motivation(
    category: str,
    validity: ValidityResult,
    features: ExtractedFeatures,
    score_result: ScoreResult,
) -> str:
    """Return a short Italian motivation for the operator."""
    if category == "invalid":
        if features.looks_invalid:
            return (
                "Lead invalido: il messaggio risulta non valido "
                "(spam/gibberish o fuori ambito), da scartare."
            )
        reasons = [
            _REASON_LABELS.get(r, r) for r in validity.reasons if not r.startswith("duplicate")
        ]
        detail = ", ".join(reasons) if reasons else "dati non utilizzabili"
        return f"Lead invalido: {detail}, da scartare."

    labels = [
        _FEATURE_LABELS.get(name, name)
        for name, _ in top_contributions(score_result, 3)
    ]
    head = f"Categoria {category} (score {score_result.score})"
    body = ", ".join(labels) if labels else "segnali di intento limitati"
    text = f"{head}: {body}."

    extra = (features.rationale_signals or "").strip()
    if extra and not extra.startswith("(mock") and not extra.startswith("(fallback"):
        text += f" {extra}."
    if score_result.low_confidence:
        text += " [estrazione a bassa confidenza]"
    return text
