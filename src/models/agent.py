"""Lead-Resolution Agent models: states, events, actions and the session.

The agent (REFACTOR_SPEC §7) is a decoupled, event-driven state machine that
lives OUTSIDE the scoring SLA. Its persisted unit of work is an
:class:`AgentSession`; each turn produces :class:`AgentAction` audit records.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from src.models.lead import ExtractedFeatures


class AgentState(str, Enum):
    """States of the Lead-Resolution state machine (§7.2).

    Recovering missing info and negotiating an appointment are the SAME machine
    with different trajectories; booking is a terminal action, not a 2nd agent.
    """

    TRIGGERED = "TRIGGERED"
    AWAITING_USER_REPLY = "AWAITING_USER_REPLY"
    EVALUATING_REPLY = "EVALUATING_REPLY"
    PROPOSING_SLOT = "PROPOSING_SLOT"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    # Non-terminal: an action is staged, waiting for human approval (§7.5).
    PENDING_APPROVAL = "PENDING_APPROVAL"
    # Terminal states
    BOOKED = "BOOKED"
    COMPLETED_INFO = "COMPLETED_INFO"
    NURTURED = "NURTURED"  # a cold lead got an automatic nurturing asset (no call)
    HANDOFF_HUMAN = "HANDOFF_HUMAN"
    DISQUALIFIED_NO_RESPONSE = "DISQUALIFIED_NO_RESPONSE"
    TERMINATED = "TERMINATED"


# Terminal states close a session: no further tool runs.
TERMINAL_STATES = frozenset(
    {
        AgentState.BOOKED,
        AgentState.COMPLETED_INFO,
        AgentState.NURTURED,
        AgentState.HANDOFF_HUMAN,
        AgentState.DISQUALIFIED_NO_RESPONSE,
        AgentState.TERMINATED,
    }
)


class AgentGoal(str, Enum):
    """Why the agent was triggered: drives the initial trajectory."""

    RECOVER_INFO = "recover_info"
    NEGOTIATE_APPOINTMENT = "negotiate_appointment"
    NURTURE = "nurturing"  # thin: send one automatic asset to a cold lead, then close


class AgentEventType(str, Enum):
    """External events that wake a persisted session."""

    START = "start"  # internal kickoff of a freshly-triggered session
    USER_REPLY = "user_reply"
    NO_RESPONSE_TIMEOUT = "no_response_timeout"
    HUMAN_APPROVAL = "human_approval"  # operator's verdict on a staged action (§7.5)


class AgentEvent(BaseModel):
    """An event delivered to the runner to advance a session one step."""

    type: AgentEventType
    text: str | None = None  # the user's reply text, for USER_REPLY
    approved: bool = True  # operator's verdict, for HUMAN_APPROVAL


class AgentAction(BaseModel):
    """A single tool invocation attempted by the agent, with its outcome.

    status:
      - "executed"         : the tool ran (mock) successfully
      - "pending_approval" : gated by decision-rights, awaiting human approval
      - "skipped"          : preconditions not met / not applicable
      - "failed"           : the tool errored (-> handoff, never inconsistent)
    """

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: str = "executed"
    reason: str = ""
    result: dict[str, Any] = Field(default_factory=dict)


class AgentSession(BaseModel):
    """Persisted state of one lead's resolution loop (event-driven, async)."""

    lead_id: str
    goal: AgentGoal
    state: AgentState = AgentState.TRIGGERED

    # Lead context captured at trigger time.
    category: str = ""
    consent: bool | None = None
    channel: str | None = None
    vehicle_interest: str | None = None
    to_token: str | None = None  # opaque recipient token (never raw PII)

    missing_fields: list[str] = Field(default_factory=list)
    proposed_slots: list[str] = Field(default_factory=list)
    chosen_slot: str | None = None

    # Score recomputed off the SLA after a recovery reply (§7.2). Persisted so the
    # operator-facing view (``finalize_with_session``) can realign category /
    # action / priority to the enriched lead once the session resolves. None until
    # the agent re-scores (negotiate / nurture goals never recompute it).
    final_score: int | None = None

    # Cached at trigger time so the agent can RE-SCORE off the SLA once it
    # recovers info (§7.2): the original extraction + the original feature vector.
    # Re-scoring overlays the re-extracted semantic values on ``base_vector`` and
    # reuses the SAME build_feature_vector mappings -> no training/serving skew,
    # no need to re-read the structured lead. ``last_reply_features`` holds the
    # most recent ``re_extract`` output for the planner to merge.
    base_features: ExtractedFeatures | None = None
    base_vector: dict[str, float] = Field(default_factory=dict)
    last_reply_features: ExtractedFeatures | None = None

    # A tool call staged by a human-approval decision, awaiting a HUMAN_APPROVAL
    # event (§7.5). Holds {"tool", "args", "next_state"}; executed only on approval.
    pending_action: dict[str, Any] | None = None

    turns: int = 0
    messages_sent: int = 0
    followups_sent: int = 0
    consent_requested: bool = False
    llm_calls: int = 0  # planner LLM calls in this session (cost ceiling, §7.4)

    actions: list[AgentAction] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES
