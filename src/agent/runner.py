"""Agent runner: create / resume / drive persisted sessions (event-driven).

Decoupled from the hot path: ``score_lead`` only sets a trigger; the runner
turns a triggered :class:`ScoredLead` into a persisted :class:`AgentSession` and
advances it on events. ``run_scripted`` drives a session to a terminal state with
SIMULATED user replies -- the mock for the demo/tests (real replies arrive async).
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.agent.session_store import InMemorySessionStore, SessionStore
from src.agent.state_machine import advance
from src.agent.tools import AgentTools
from src.config import Settings, get_settings
from src.models.agent import AgentEvent, AgentEventType, AgentGoal, AgentSession
from src.models.lead import Lead
from src.models.output import ScoredLead
from src.privacy import email_key, phone_key
from src.scoring.feature_vector import build_feature_vector


class AgentRunner:
    """Creates, persists and advances Lead-Resolution sessions."""

    def __init__(
        self,
        tools: AgentTools | None = None,
        store: SessionStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.tools = tools or AgentTools()
        self.store = store or InMemorySessionStore()

    def start_session(self, scored: ScoredLead, lead: Lead) -> AgentSession | None:
        """Create + persist a session for a triggered lead and run its kickoff."""
        if not scored.agent_triggered or not scored.agent_goal:
            return None
        to_token = phone_key(lead.phone) or email_key(lead.email)
        now = datetime.now(timezone.utc)
        # Cache the original extraction + feature vector so the agent can re-score
        # off the SLA after recovering info (§7.2), without re-reading the lead.
        base_vector = build_feature_vector(scored.features, lead, now).values
        session = AgentSession(
            lead_id=scored.lead_id,
            goal=AgentGoal(scored.agent_goal),
            category=scored.category,
            consent=lead.consent,
            channel=lead.channel,
            vehicle_interest=lead.vehicle_interest,
            to_token=to_token,
            missing_fields=list(scored.features.missing_critical_fields),
            base_features=scored.features,
            base_vector=dict(base_vector),
            created_at=now,
            updated_at=now,
        )
        advance(session, AgentEvent(type=AgentEventType.START), self.tools, self._settings)
        self._persist(session)
        return session

    def resume_on_reply(self, lead_id: str, event: AgentEvent) -> AgentSession | None:
        """Wake a persisted session with an incoming event (reply / timeout)."""
        session = self.store.get(lead_id)
        if session is None or session.is_terminal:
            return session
        advance(session, event, self.tools, self._settings)
        self._persist(session)
        return session

    def run_scripted(
        self, scored: ScoredLead, lead: Lead, replies: list[AgentEvent]
    ) -> AgentSession | None:
        """Drive a session to a terminal state with simulated events (demo/tests)."""
        session = self.start_session(scored, lead)
        if session is None:
            return None
        for event in replies:
            if session.is_terminal:
                break
            advance(session, event, self.tools, self._settings)
        self._persist(session)
        return session

    def _persist(self, session: AgentSession) -> None:
        session.updated_at = datetime.now(timezone.utc)
        self.store.save(session)


def user_reply(text: str) -> AgentEvent:
    """Convenience: an incoming user-reply event."""
    return AgentEvent(type=AgentEventType.USER_REPLY, text=text)


def no_response() -> AgentEvent:
    """Convenience: a response-timeout event."""
    return AgentEvent(type=AgentEventType.NO_RESPONSE_TIMEOUT)


def human_approval(approved: bool = True) -> AgentEvent:
    """Convenience: an operator's verdict on a staged action (e.g. a booking)."""
    return AgentEvent(type=AgentEventType.HUMAN_APPROVAL, approved=approved)
