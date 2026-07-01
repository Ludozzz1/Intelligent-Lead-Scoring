"""Operator-facing suggestions: next-best-action + queue routing + agent status.

Three concerns, all deterministic (NO LLM — the hot path stays at a single call):

* ``classify_queue`` — which operator bucket a scored lead belongs to:
  ``attiva`` (the operator must call) / ``agente`` (auto-handled, no call) /
  ``scartato`` (invalid: dropped from the call queue, kept only for audit). This
  is what makes ``scartare`` a *system disposition* instead of a useless "please
  discard" suggestion to a human.
* ``build_next_best_action`` — a short "what to do now" from a CLOSED vocabulary
  specific to a car dealership. It is the human mirror of the agent's tool belt
  (``estimate_trade_in`` -> "valuta la permuta", ``check_availability`` ->
  "proponi il test drive", ...): without consent the operator runs the lever
  manually, with consent the agent runs it (so the suggestion says "non chiamare").
* ``finalize_with_session`` — once the async agent resolves, realign the lead's
  operator-facing fields (action, suggestion, queue, status) AND its enriched
  score/category/priority to the real outcome (booked / info recovered / handoff
  / no-response). Leads that need a human re-emerge in the ``attiva`` queue.

UI copy is Italian and lives here as constants (like ``motivation.py``);
``config/`` holds only numeric artifacts.
"""

from __future__ import annotations

from src.action.decision import (
    ACTION_ASK_INFO,
    ACTION_NURTURE,
    ACTION_VALID,
    _compute_priority,
)
from src.models.agent import AgentSession, AgentState
from src.models.lead import ExtractedFeatures
from src.models.output import ScoredLead

# -- queue buckets -----------------------------------------------------------

QUEUE_ACTIVE = "attiva"
QUEUE_AGENT = "agente"
QUEUE_DISCARDED = "scartato"


def classify_queue(category: str, agent_triggered: bool) -> str:
    """Route a scored lead to an operator bucket (``attiva|agente|scartato``)."""
    if category == "invalid":
        return QUEUE_DISCARDED
    return QUEUE_AGENT if agent_triggered else QUEUE_ACTIVE


# -- closed suggestion vocabulary (car dealership) ---------------------------

_DISCARD_SUGGESTION = "Scartato in automatico — nessuna chiamata."

# Per-category opener for an operator-callable lead.
_CATEGORY_PREFIX = {
    "hot": "Chiama subito (priorità alta)",
    "warm": "Chiama (priorità media)",
    "cold": "Bassa priorità / nurturing",
}

# What the agent is doing when it owns the lead (keyed by AgentGoal value).
_AGENT_GOAL_PENDING = {
    "recover_info": "In gestione dall'agente: recupero info mancanti — non chiamare.",
    "negotiate_appointment": "In gestione dall'agente: proposta appuntamento — non chiamare.",
    "nurturing": "Nurturing automatico in corso — nessuna chiamata.",
}
_AGENT_GOAL_PENDING_DEFAULT = "In gestione dall'agente — non chiamare."

# ``missing_critical_fields`` keys (prompt + fixtures) -> noun fragment to ask.
_MISSING_LABELS = {
    "budget": "il budget",
    "timeline_acquisto": "i tempi d'acquisto",
    "modello": "il modello d'interesse",
}


def _format_budget(value: float | None) -> str:
    if not value:
        return ""
    if value >= 1000:
        return f"~{int(round(value / 1000))}k€"
    return f"~{int(value)}€"


def _collect_asks(missing_fields: list[str]) -> list[str]:
    """Operator-facing fragments for the info still missing to qualify."""
    return [_MISSING_LABELS.get(f, f) for f in missing_fields]


def _collect_levers(features: ExtractedFeatures) -> list[str]:
    """Commercial levers (the human mirror of the agent tool belt), fixed order."""
    levers: list[str] = []
    if features.availability_mentioned:
        levers.append("proponi il test drive")
    if features.trade_in_present:
        levers.append(
            f"valuta la permuta ({features.trade_in_vehicle})"
            if features.trade_in_vehicle
            else "valuta la permuta"
        )
    if features.budget_present:
        budget = _format_budget(features.budget_value_eur)
        levers.append(
            f"budget {budget}, proponi finanziamento" if budget else "proponi finanziamento"
        )
    else:
        levers.append("verifica il budget")
    if features.vehicle_specificity in ("generic", "none"):
        levers.append("conferma il modello d'interesse")
    return levers


def build_next_best_action(
    category: str,
    recommended_action: str,
    features: ExtractedFeatures,
    agent_triggered: bool,
    agent_goal: str | None = None,
) -> str:
    """Return the operator's next-best-action (closed vocabulary, deterministic).

    Framed by the queue bucket: a discarded lead needs no call; an agent-owned
    lead says "non chiamare"; an operator-callable lead gets a category opener
    plus up to two concrete commercial levers (or the info to ask if incomplete).
    """
    queue = classify_queue(category, agent_triggered)
    if queue == QUEUE_DISCARDED:
        return _DISCARD_SUGGESTION
    if queue == QUEUE_AGENT:
        return _AGENT_GOAL_PENDING.get(agent_goal or "", _AGENT_GOAL_PENDING_DEFAULT)

    if recommended_action == ACTION_ASK_INFO:
        asks = _collect_asks(features.missing_critical_fields)
        if asks:
            return f"Chiedi le info mancanti: {', '.join(asks)}."
        return "Chiedi le informazioni mancanti per qualificare il lead."

    prefix = _CATEGORY_PREFIX.get(category, "Contatta il lead")
    levers = _collect_levers(features)[:2]
    if levers:
        return f"{prefix} — {'; '.join(levers)}."
    return f"{prefix}."


# -- agent outcome -> operator realignment -----------------------------------

_AGENT_STATUS_LABELS = {
    AgentState.TRIGGERED: "Agente avviato",
    AgentState.AWAITING_USER_REPLY: "Agente in attesa di risposta dal cliente",
    AgentState.EVALUATING_REPLY: "Agente sta valutando la risposta",
    AgentState.PROPOSING_SLOT: "Agente sta proponendo gli slot",
    AgentState.AWAITING_CONFIRMATION: "Agente in attesa di conferma slot",
    AgentState.PENDING_APPROVAL: "Prenotazione in attesa di approvazione operatore",
    AgentState.BOOKED: "Appuntamento prenotato",
    AgentState.COMPLETED_INFO: "Info recuperate dall'agente",
    AgentState.NURTURED: "Nurturing completato",
    AgentState.HANDOFF_HUMAN: "Handoff a operatore umano",
    AgentState.DISQUALIFIED_NO_RESPONSE: "Nessuna risposta dal cliente",
    AgentState.TERMINATED: "Sessione agente chiusa",
}


def agent_status_label(state: AgentState) -> str:
    """Operator-facing label for an agent lifecycle state."""
    return _AGENT_STATUS_LABELS.get(state, getattr(state, "value", str(state)))


# (queue, recommended_action, next_best_action) per terminal / staged state.
# Leads that need a human re-emerge in the ``attiva`` queue with takeover context;
# ``BOOKED`` / ``NURTURED`` close in ``agente`` (no operator call).
_CLOSURE = {
    AgentState.BOOKED: (
        QUEUE_AGENT, ACTION_VALID,
        "Appuntamento prenotato dall'agente — nessuna chiamata necessaria.",
    ),
    AgentState.PENDING_APPROVAL: (
        QUEUE_ACTIVE, ACTION_VALID,
        "Approva la prenotazione predisposta dall'agente.",
    ),
    AgentState.COMPLETED_INFO: (
        QUEUE_ACTIVE, ACTION_VALID,
        "Info recuperate dall'agente — chiama, lead qualificato.",
    ),
    AgentState.NURTURED: (
        QUEUE_AGENT, ACTION_NURTURE,
        "Nurturing automatico inviato — nessuna chiamata.",
    ),
    AgentState.HANDOFF_HUMAN: (
        QUEUE_ACTIVE, ACTION_VALID,
        "Handoff dall'agente — riprendi tu e chiama.",
    ),
    AgentState.DISQUALIFIED_NO_RESPONSE: (
        QUEUE_ACTIVE, ACTION_VALID,
        "Nessuna risposta ai contatti automatici — ultimo tentativo manuale o chiudi.",
    ),
}


def finalize_with_session(scored: ScoredLead, session: AgentSession) -> ScoredLead:
    """Realign a scored lead to the agent's outcome (pure, like ``_as_duplicate``).

    Reflects the resolved session onto the operator-facing fields and onto the
    enriched score/category/priority (when the agent re-scored during recovery,
    §7.2). The SLA-time ``ScoredLead`` is unchanged; this is the later view the
    operator surfaces / monolith consume once the agent has resolved.
    """
    state = session.state
    category = session.category or scored.category
    score = session.final_score if session.final_score is not None else scored.score

    closure = _CLOSURE.get(state)
    if closure is not None:
        queue, recommended_action, next_best = closure
    else:
        # Still in flight: the agent owns the lead, the operator should not call.
        queue = QUEUE_AGENT
        recommended_action = scored.recommended_action
        next_best = _AGENT_GOAL_PENDING.get(
            session.goal.value, _AGENT_GOAL_PENDING_DEFAULT
        )

    priority = _compute_priority(category, score, scored.personalization)
    return scored.model_copy(
        update={
            "agent_session": session,
            "agent_status": agent_status_label(state),
            "recommended_action": recommended_action,
            "next_best_action": next_best,
            "queue": queue,
            "category": category,
            "score": score,
            "priority": priority,
        }
    )
