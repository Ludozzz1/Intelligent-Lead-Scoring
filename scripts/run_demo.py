"""End-to-end, deterministic demo of the two-zone architecture (no API key).

HOT PATH (zone 1):
    leads_mock.json -> InMemoryQueue (SQS stand-in) -> consume_all (DLQ for
    poison) -> Pipeline.score_lead (ONE LLM call) -> MockMonolithCallback.

AGENTIC ZONE (zone 2), decoupled and event-driven:
    each triggered lead becomes an AgentSession the runner drives to a terminal
    state with SIMULATED user replies (booked / completed_info / handoff /
    disqualified_no_response).

Run::

    py -3 scripts/run_demo.py
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from src.action.suggestions import (
    QUEUE_ACTIVE,
    QUEUE_AGENT,
    QUEUE_DISCARDED,
    finalize_with_session,
)
from src.agent.runner import AgentRunner, human_approval, no_response, user_reply
from src.integrations.monolith_callback import MockMonolithCallback
from src.integrations.queue import DeadLetterQueue, InMemoryQueue, consume_all
from src.models.lead import Lead
from src.pipeline import get_pipeline

_REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 6, 29, 12, 0, 0)


def _scripted_replies(scored, lead) -> list:
    """Deterministic simulated replies that showcase every terminal state."""
    if lead.consent is not True:
        return []  # cannot auto-message -> no agent (operator handles it)
    if scored.lead_id.endswith("0070"):
        return [no_response()]  # show the no-response disqualification
    confirm = [user_reply("Va bene sabato, confermo il test drive."), human_approval()]
    if scored.agent_goal == "recover_info":
        # Recover the missing info: the agent RE-SCORES the reply, gets promoted to
        # booking-worthy, proposes slots -> then the user confirms and books (§7.2).
        return [
            user_reply("Il mio budget è 25000 euro, vorrei comprare entro un mese."),
            *confirm,
        ]
    # Confirm a slot -> booking is staged (PENDING_APPROVAL) -> operator approves.
    return confirm


def main() -> None:
    pipeline = get_pipeline()
    callback = MockMonolithCallback()
    runner = AgentRunner()

    leads = json.loads((_REPO / "data" / "leads_mock.json").read_text(encoding="utf-8"))
    queue = InMemoryQueue(list(leads))
    dlq = DeadLetterQueue()

    scored_by_id: dict[str, object] = {}
    leads_by_id = {ld["lead_id"]: Lead(**ld) for ld in leads}

    def handle(item: dict) -> None:
        scored = pipeline.score_lead(Lead(**item), now=NOW)
        callback.send_score(scored)
        scored_by_id[scored.lead_id] = scored

    print("=" * 78)
    print("ZONA 1 — HOT PATH (deterministico, 1 sola call LLM)")
    print("=" * 78)
    processed = asyncio.run(consume_all(queue, handle, dlq))
    print(f"Processati {processed} lead dalla coda (DLQ: {len(dlq)}).")

    snapshot = list(scored_by_id.values())
    cats = {c: sum(1 for s in snapshot if s.category == c) for c in ("hot", "warm", "cold", "invalid")}
    avg_latency = sum(s.latency_ms for s in snapshot) / max(1, len(snapshot))
    print(f"Distribuzione: {cats} | latenza media {avg_latency:.1f} ms\n")

    print("=" * 78)
    print("ZONA 2 — AGENTE (decoupled, event-driven, repliche simulate)")
    print("=" * 78)
    triggered = [s for s in snapshot if s.agent_triggered]
    print(f"{len(triggered)} lead hanno attivato l'agente.\n")
    for s in triggered:
        lead = leads_by_id[s.lead_id]
        session = runner.run_scripted(s, lead, _scripted_replies(s, lead))
        if session is None:
            continue
        # Realign the operator-facing view to the agent's resolved outcome.
        scored_by_id[s.lead_id] = finalize_with_session(s, session)
        callback.send_agent_outcome(s.lead_id, session.state.value)
        tools = " -> ".join(f"{a.tool}[{a.status}]" for a in session.actions)
        print(f"{s.lead_id} ({s.agent_goal}) => {session.state.value}")
        print(f"    {tools}\n")

    # VISTA OPERATORE — partition by queue once the agent has resolved.
    ordered = sorted(scored_by_id.values(), key=lambda s: s.priority, reverse=True)
    active = [s for s in ordered if s.queue == QUEUE_ACTIVE]
    agent_q = [s for s in ordered if s.queue == QUEUE_AGENT]
    discarded = [s for s in ordered if s.queue == QUEUE_DISCARDED]

    print("=" * 78)
    print("VISTA OPERATORE — coda di chiamata (priorità desc)")
    print("=" * 78)
    print(f"{'lead_id':10} {'cat':6} {'score':>5} {'prio':>4} {'conf':4}  prossima azione")
    print("-" * 78)
    for s in active:
        lc = "low" if s.low_confidence else "ok"
        print(f"{s.lead_id:10} {s.category:6} {s.score:5} {s.priority:4} {lc:4}  {s.next_best_action}")

    print(f"\nGestiti dall'agente (nessuna chiamata operatore): {len(agent_q)}")
    for s in agent_q:
        print(f"  {s.lead_id:10} {s.recommended_action:12} {s.agent_status} — {s.next_best_action}")

    print(f"\nScartati in automatico (fuori dalla coda di chiamata): {len(discarded)}")
    for s in discarded:
        print(f"  {s.lead_id:10} {s.motivation}")

    avoided = len(discarded) + len(agent_q)
    total = len(ordered)
    print(f"\nChiamate operatore evitate ~ {avoided}/{total} = {avoided / max(1, total):.0%} "
          f"(scartati di sistema + gestiti dall'agente; vedi docs/cost_model.md).")

    sample = active[0] if active else ordered[0]
    print("\n" + "=" * 78)
    print(f"Esempio output completo — {sample.lead_id}")
    print("=" * 78)
    payload = sample.model_dump(mode="json", exclude_none=True)
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:1600])


if __name__ == "__main__":
    main()
