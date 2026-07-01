"""Final per-lead output contract produced by the hot path.

Changes vs the legacy contract: the separate ``risk`` axis and the 4-bucket
``quality_breakdown`` are gone. The score now carries its own per-feature
``contributions`` (:class:`ScoreResult`); confidence is ``low_confidence``
(from the extraction). The agent is decoupled, so ``score_lead`` returns
immediately with at most a trigger -- the full trajectory lives in the agent
session store and is attached later by the runner (demo/CLI).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from src.models.agent import AgentSession
from src.models.features import ScoreResult
from src.models.lead import ExtractedFeatures
from src.models.scoring import Personalization, ValidityResult


class ScoredLead(BaseModel):
    """Complete, explainable scoring result for a single lead."""

    lead_id: str
    score: int = 0
    # "hot" | "warm" | "cold" | "invalid"
    category: str = "invalid"

    validity: ValidityResult
    features: ExtractedFeatures = Field(default_factory=ExtractedFeatures)
    score_result: ScoreResult = Field(default_factory=ScoreResult)

    # Deterministic, natural-language motivation (no LLM explainer call).
    motivation: str = ""
    # "lead_valido" | "chiedere_info" | "nurturing" | "scartare" (REFACTOR_SPEC §5.6).
    recommended_action: str = "scartare"
    # Operator-facing "what to do now" from a closed deterministic vocabulary
    # (the WHY stays in ``motivation``). Mirrors the agent's tool belt so the
    # manual and the automatic playbook are the same thing seen from two sides.
    next_best_action: str = ""
    # Operator queue bucket: "attiva" (operator must call) | "agente"
    # (auto-handled, no call) | "scartato" (invalid, dropped from the call queue).
    queue: str = "attiva"
    # Operator-facing agent lifecycle label; set once the session resolves
    # (``finalize_with_session``), empty at scoring time (the agent is decoupled).
    agent_status: str = ""

    personalization: Personalization = Field(default_factory=Personalization)

    # Whether the lead was handed to the (async) agentic zone, and why.
    agent_triggered: bool = False
    agent_goal: str | None = None
    # Attached by the agent runner once the session has progressed (demo/CLI);
    # None at scoring time because the agent is decoupled from the SLA path.
    agent_session: AgentSession | None = None

    priority: int = 0
    low_confidence: bool = False
    processed_at: datetime | None = None
    latency_ms: int = 0
