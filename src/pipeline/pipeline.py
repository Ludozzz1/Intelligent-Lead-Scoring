"""Hot-path orchestration (deterministic, <=2 min, ONE LLM call).

Stage order (each stage guarded so the worst case degrades, never raises):

    1. derive a stable ``lead_id`` + idempotency cache;
    2. validation gate (structural, no LLM) -> invalid STOPs before any LLM call;
    3. personalization (exact-match history: dedup / returning customer);
    4. extraction -- the SINGLE LLM call (gated, PII-redacted), or fallback;
    5. build_feature_vector (§5.3, shared) -> compute_score (linear, naive weights);
    6. categorization (bands) with looks_invalid override;
    7. deterministic motivation (no 2nd LLM call);
    8. action decision + agent trigger (the agent runs DECOUPLED, not here).

HARD INVARIANT: the LLM only produces ``ExtractedFeatures``; it is never in the
score arithmetic. A slow/failing LLM degrades the semantic feature values (and
flags ``low_confidence``) but can never break or delay the score past the SLA.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from functools import lru_cache

from src.action.decision import ACTION_ASK_INFO, decide_action
from src.action.suggestions import build_next_best_action, classify_queue
from src.categorization.bands import categorize
from src.config import Settings, get_settings
from src.extraction.extractor import extract_features
from src.extraction.llm import LLMAdapter
from src.gate.validity import evaluate_validity
from src.history import HistoryService
from src.logging_setup import configure_logging
from src.models.features import ScoreResult
from src.models.lead import ExtractedFeatures, Lead
from src.models.output import ScoredLead
from src.models.scoring import Personalization, ValidityResult
from src.motivation.motivation import build_motivation
from src.privacy import email_key, phone_key
from src.scoring.feature_vector import build_feature_vector
from src.scoring.scorer import compute_score


class Pipeline:
    """Stateful single-threaded scoring pipeline with cached singletons."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._log = configure_logging()
        self._history = HistoryService(self._settings)
        self._llm = LLMAdapter(self._settings)
        self._processed: dict[str, ScoredLead] = {}
        self._log.info(
            "Pipeline ready (history_records=%d, llm_mode=%s)",
            self._history.record_count,
            self._llm.mode,
        )

    # -- public API ----------------------------------------------------------

    def score_lead(self, lead: Lead, now: datetime | None = None) -> ScoredLead:
        """Score one lead into a complete ``ScoredLead`` (never raises)."""
        start = time.perf_counter()
        lead_id = self._derive_lead_id(lead)

        cached = self._processed.get(lead_id)
        if cached is not None:
            self._log.info("Cache hit for lead_id=%s (duplicate)", lead_id)
            return self._as_duplicate(cached)

        try:
            scored = self._score_uncached(lead, lead_id, start, now)
        except Exception as exc:  # noqa: BLE001 - total: never raise to caller
            self._log.exception("Scoring failed for lead_id=%s: %r", lead_id, exc)
            scored = self._fallback_invalid(lead_id, start)

        self._processed[lead_id] = scored
        return scored

    @property
    def history(self) -> HistoryService:
        return self._history

    @property
    def settings(self) -> Settings:
        return self._settings

    # -- core flow -----------------------------------------------------------

    def _score_uncached(
        self, lead: Lead, lead_id: str, start: float, now: datetime | None
    ) -> ScoredLead:
        validity = self._safe(
            lambda: evaluate_validity(lead, self._history, self._settings),
            ValidityResult(is_valid=False, failure_type="invalid", reasons=["gate_error"]),
            "validity",
        )
        personalization = self._safe(
            lambda: self._history.personalize(lead), Personalization(), "personalize"
        )
        features = self._safe(
            lambda: extract_features(lead, validity, self._llm),
            ExtractedFeatures(extraction_source="none"),
            "extraction",
        )

        vector = build_feature_vector(features, lead, now)
        score_result = self._safe(
            lambda: compute_score(vector, features, self._settings),
            ScoreResult(),
            "scorer",
        )

        category = categorize(
            score_result.score, validity.is_valid, features.looks_invalid, self._settings
        )
        final_score = 0 if category == "invalid" else score_result.score

        action = decide_action(
            category, validity, features, final_score, personalization,
            consent=lead.consent, settings=self._settings,
        )
        motivation = build_motivation(category, validity, features, score_result)

        agent_triggered = action.agent_goal is not None
        agent_goal = action.agent_goal.value if action.agent_goal else None
        queue = classify_queue(category, agent_triggered)
        next_best_action = build_next_best_action(
            category, action.recommended_action, features, agent_triggered, agent_goal,
        )

        latency_ms = int((time.perf_counter() - start) * 1000)
        scored = ScoredLead(
            lead_id=lead_id,
            score=final_score,
            category=category,
            validity=validity,
            features=features,
            score_result=score_result,
            motivation=motivation,
            recommended_action=action.recommended_action,
            next_best_action=next_best_action,
            queue=queue,
            personalization=personalization,
            agent_triggered=agent_triggered,
            agent_goal=agent_goal,
            priority=action.priority,
            low_confidence=features.low_confidence,
            processed_at=datetime.now(timezone.utc),
            latency_ms=latency_ms,
        )
        self._log.info(
            "Scored lead_id=%s category=%s score=%d priority=%d action=%s "
            "agent=%s src=%s latency_ms=%d",
            lead_id, category, final_score, action.priority,
            action.recommended_action, scored.agent_goal,
            features.extraction_source, latency_ms,
        )
        return scored

    # -- helpers -------------------------------------------------------------

    def _safe(self, fn, default, stage: str):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Stage %s failed: %r", stage, exc)
            return default

    def _derive_lead_id(self, lead: Lead) -> str:
        if lead.lead_id:
            return lead.lead_id
        pk = phone_key(lead.phone) or ""
        ek = email_key(lead.email) or ""
        created = lead.created_at.isoformat() if lead.created_at else ""
        digest = hashlib.sha256(f"{pk}|{ek}|{created}".encode("utf-8")).hexdigest()[:16]
        return f"LEAD-{digest}"

    @staticmethod
    def _as_duplicate(scored: ScoredLead) -> ScoredLead:
        personalization = scored.personalization.model_copy(update={"is_duplicate": True})
        return scored.model_copy(update={"personalization": personalization})

    def _fallback_invalid(self, lead_id: str, start: float) -> ScoredLead:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ScoredLead(
            lead_id=lead_id,
            score=0,
            category="invalid",
            validity=ValidityResult(
                is_valid=False, failure_type="invalid", reasons=["pipeline_error"]
            ),
            motivation="Lead invalido: errore nella pipeline, da rivedere.",
            recommended_action="scartare",
            next_best_action=build_next_best_action(
                "invalid", "scartare", ExtractedFeatures(), False
            ),
            queue=classify_queue("invalid", False),
            low_confidence=True,
            processed_at=datetime.now(timezone.utc),
            latency_ms=latency_ms,
        )


@lru_cache
def get_pipeline() -> Pipeline:
    """Return a cached singleton ``Pipeline`` built from the active settings."""
    return Pipeline(get_settings())
