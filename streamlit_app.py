"""Streamlit review UI per la pipeline di lead scoring (mock-first, no API key).

Riproduce la chiamata che il monolite Java fa al servizio: l'operatore sceglie un
lead (campione o caricato), ne esegue lo scoring in-process (``pipeline.score_lead``)
e — se il lead attiva l'agente — ne vede la conversazione event-driven con repliche
del lead SIMULATE, rendendo leggibili i passaggi e le decisioni di entrambe le zone.

Al login non parte alcuna analisi: l'utente entra e sceglie esplicitamente se avviare
con il lead mock o caricarne uno. Il consenso al contatto (non presente nei feed reali)
è impostato a ON, con un toggle per testare anche il ramo senza consenso.

Run::

    ./.venv/Scripts/python.exe -m streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import streamlit as st

from src.action.suggestions import agent_status_label
from src.agent.runner import AgentRunner, human_approval, no_response, user_reply
from src.config import get_settings
from src.models.agent import AgentAction, AgentEvent, AgentEventType, AgentState
from src.models.lead import Lead
from src.models.output import ScoredLead
from src.pipeline import get_pipeline

# Fixed evaluation clock: keeps recency (and therefore scores) reproducible,
# aligned with cli.py / scripts/run_demo.py.
NOW = datetime(2026, 6, 29, 12, 0, 0)
DEMO_PASSWORD = "autoxy-demo"
DEMO_LEAD_ID = "LEAD-0001"  # SUV ibrido 35k (schema canonico): attiva l'agente.


# --- human-readable labels --------------------------------------------------
# Codici interni (azioni, obiettivi, tool) -> etichette parlanti per l'operatore.
# La UI non mostra mai gli identificatori grezzi.

_ACTION_LABELS = {
    "lead_valido": "Lead valido",
    "chiedere_info": "Chiedere info mancanti",
    "nurturing": "Nurturing (bassa priorità)",
    "scartare": "Scartare",
}

_GOAL_LABELS = {
    "recover_info": "recupero info mancanti",
    "negotiate_appointment": "proposta appuntamento",
}

# Nome del tool agentico -> cosa l'LLM ha deciso di fare (step del transcript).
_TOOL_LABELS = {
    "re_extract": "rileggere la risposta del cliente",
    "check_availability": "verificare gli slot per il test drive",
    "book_appointment": "prenotare l'appuntamento",
    "estimate_trade_in": "valutare la permuta",
    "check_inventory": "verificare la disponibilità del veicolo",
    "recommend_alternatives": "proporre veicoli alternativi",
    "simulate_financing": "simulare il finanziamento",
    "schedule_followup": "programmare un follow-up",
    "update_crm": "aggiornare il CRM",
    "warm_transfer_to_operator": "passare il lead a un operatore",
    "escalate_to_human": "passare il lead a un operatore",
}

# Prefisso con cui il planner LLM marca le azioni pianificate (planner.py).
_LLM_PLAN_PREFIX = "pianificato dall'LLM:"


def _action_label(code: str) -> str:
    return _ACTION_LABELS.get(code, code)


def _goal_label(code: str | None) -> str:
    return _GOAL_LABELS.get(code or "", code or "")


# --- chrome -----------------------------------------------------------------

def _inject_css() -> None:
    """Nasconde l'hint 'Press Enter to apply' che copre l'occhiello del password."""
    st.markdown(
        "<style>[data-testid='InputInstructions']{display:none;}</style>",
        unsafe_allow_html=True,
    )


# --- auth -------------------------------------------------------------------

def _require_login() -> None:
    """Gate the app behind a shared password (env APP_PASSWORD, default demo)."""
    if st.session_state.get("authed"):
        return
    expected = os.environ.get("APP_PASSWORD", DEMO_PASSWORD)
    st.title("Lead Scoring")
    st.caption("Accesso riservato al call center.")
    pw = st.text_input("Password", type="password", placeholder="Password")
    if not pw:
        st.stop()
    if pw != expected:
        st.error("Password errata.")
        st.stop()
    st.session_state["authed"] = True
    st.rerun()


# --- input ------------------------------------------------------------------

def _load_samples() -> dict[str, dict]:
    """Bundled sample leads (data/leads_mock.json), keyed by lead_id."""
    path = Path(get_settings().leads_mock_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, list):
        return {}
    return {str(r.get("lead_id") or f"lead-{i}"): r for i, r in enumerate(raw)}


def _parse_upload(up) -> dict | None:
    """Parse an uploaded JSON file into a single lead dict (one lead per file)."""
    if up is None:
        return None
    try:
        data = json.loads(up.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        st.error(f"JSON non valido: {exc}")
        return None
    if isinstance(data, dict):
        return data
    st.error("Il file deve contenere un singolo lead (un oggetto JSON).")
    return None


def _current_lead() -> dict | None:
    """The lead selected in this session, or None while on the landing screen."""
    source = st.session_state.get("lead_source")
    if source == "mock":
        return _load_samples().get(st.session_state.get("sample_id"))
    if source == "upload":
        return st.session_state.get("upload_lead")
    return None


def _start_screen() -> None:
    """Landing: nothing runs until the user picks the mock lead or uploads one."""
    st.title("Lead Scoring")
    st.caption(
        "Valuta un lead in ingresso: scoring hot-path e, se previsto, l'agente di "
        "risoluzione. Scegli come iniziare."
    )
    st.write("")
    c1, c2 = st.columns(2)
    start_mock = c1.button("Avvia con lead mock", type="primary", use_container_width=True)
    start_upload = c2.button("Carica un lead", use_container_width=True)

    if start_mock:
        samples = _load_samples()
        if not samples:
            st.error("Nessun lead campione in data/leads_mock.json.")
            st.stop()
        st.session_state["lead_source"] = "mock"
        st.session_state["sample_id"] = (
            DEMO_LEAD_ID if DEMO_LEAD_ID in samples else next(iter(samples))
        )
        st.rerun()

    if start_upload:
        st.session_state["show_upload"] = True

    if st.session_state.get("show_upload"):
        st.write("")
        up = st.file_uploader("File lead (.json)", type=["json"])
        candidate = _parse_upload(up)
        if candidate is not None and st.button("Analizza lead", type="primary"):
            st.session_state["lead_source"] = "upload"
            st.session_state["upload_lead"] = candidate
            st.session_state.pop("show_upload", None)
            st.rerun()


def _sidebar_controls(lead_dict: dict) -> dict:
    """Slim sidebar: back to landing, quick sample switch, consent toggle."""
    sb = st.sidebar
    if sb.button("Nuovo lead", use_container_width=True):
        for key in ("lead_source", "sample_id", "upload_lead", "show_upload", "agent"):
            st.session_state.pop(key, None)
        st.rerun()
    sb.divider()

    if st.session_state.get("lead_source") == "mock":
        samples = _load_samples()
        ids = list(samples)
        current = st.session_state.get("sample_id", ids[0] if ids else None)
        picked = sb.selectbox(
            "Lead campione", ids, index=ids.index(current) if current in ids else 0
        )
        if picked != current:
            st.session_state["sample_id"] = picked
            st.rerun()
        lead_dict = samples.get(picked, lead_dict)

    return _apply_consent(lead_dict, sb)


def _apply_consent(lead_dict: dict, sb) -> dict:
    """Consent toggle: auto-ON when the lead omits it (real feeds do not send it)."""
    missing = lead_dict.get("consent") is None
    default = True if missing else bool(lead_dict.get("consent"))
    consent = sb.toggle("Consenso al contatto", value=default)
    if missing:
        sb.caption(
            "Consenso non presente nel lead: impostato da questo strumento. "
            "Spegnilo per simulare il ramo senza consenso (operatore)."
        )
    return {**lead_dict, "consent": consent}


# --- scoring view -----------------------------------------------------------

def _render_scoring(scored: ScoredLead) -> None:
    c1, c2 = st.columns(2)
    c1.metric("Score", scored.score)
    c2.metric("Categoria", scored.category)
    if scored.low_confidence:
        st.warning("Estrazione a bassa confidenza: segnali semantici limitati.")

    st.markdown("**Motivazione dello score**")
    st.info(scored.motivation)
    st.markdown(
        f"**Azione consigliata:** {_action_label(scored.recommended_action)}  \n"
        f"Cosa fare ora: {scored.next_best_action}"
    )
    trig = "sì" if scored.agent_triggered else "no"
    goal = f" · obiettivo: {_goal_label(scored.agent_goal)}" if scored.agent_goal else ""
    st.caption(f"Agente attivato: {trig}{goal}")


# --- agent conversation view ------------------------------------------------

_OUTBOUND_TOOLS = frozenset({"send_message", "send_asset", "capture_consent"})


def _outbound_text(a: AgentAction) -> str:
    """The natural-language message the agent generated for the user."""
    text = (a.args.get("text") or "").strip()
    if text:
        return text
    if a.tool == "capture_consent":
        return "_Richiesta di consenso (double opt-in) inviata._"
    if a.tool == "send_asset":
        return f"_Materiale inviato: {a.args.get('asset_type', 'documento')}._"
    return a.reason or a.tool


def _step_label(a: AgentAction) -> str:
    """Human-readable transcript step for a tool action.

    An LLM-planned action carries ``reason = "pianificato dall'LLM: <tool>"``
    (planner.py): we surface it as "L'LLM ha deciso di <etichetta parlante>".
    Deterministic-fallback actions already ship a spoken Italian rationale, so we
    keep it verbatim.
    """
    reason = (a.reason or "").strip()
    if reason.startswith(_LLM_PLAN_PREFIX):
        tool = reason[len(_LLM_PLAN_PREFIX):].strip() or a.tool
        return f"L'LLM ha deciso di {_TOOL_LABELS.get(tool, tool)}"
    return reason or _TOOL_LABELS.get(a.tool, a.tool)


def _action_items(actions: list[AgentAction]) -> list[tuple[str, str]]:
    """Turn agent actions into transcript items: messages as bubbles, rest as steps."""
    items: list[tuple[str, str]] = []
    for a in actions:
        if a.tool in _OUTBOUND_TOOLS:
            items.append(("assistant", _outbound_text(a)))
        elif a.tool == "escalate_to_human":
            items.append(("step", f"handoff a operatore umano — {a.reason}"))
        else:
            suffix = " · in attesa di approvazione" if a.status == "pending_approval" else ""
            items.append(("step", f"{_step_label(a)}{suffix}"))
    return items


def _event_item(event: AgentEvent) -> tuple[str, str]:
    """Turn an external event into a transcript item."""
    if event.type == AgentEventType.USER_REPLY:
        return ("user", event.text or "")
    if event.type == AgentEventType.HUMAN_APPROVAL:
        verdict = "approva" if event.approved else "rifiuta"
        return ("system", f"Operatore: {verdict} l'azione predisposta")
    return ("system", "Il lead non ha risposto entro la finestra")


def _render_transcript(items: list[tuple[str, str]]) -> None:
    """Render the accumulated conversation transcript in order."""
    for kind, text in items:
        if kind in ("assistant", "user"):
            with st.chat_message(kind):
                st.markdown(text)
        elif kind == "step":
            st.caption(f"→ {text}")
        else:  # system
            st.caption(f"— {text} —")


def _advance(ag: dict, event: AgentEvent) -> None:
    """Apply one event to the live session and append what happened to the transcript."""
    ag["transcript"].append(_event_item(event))
    updated = ag["runner"].resume_on_reply(ag["lead_id"], event)
    if updated is not None:
        ag["transcript"].extend(_action_items(updated.actions[ag["seen"]:]))
        ag["seen"] = len(updated.actions)


def _render_outcome(session) -> None:
    st.divider()
    st.markdown(f"**Esito:** `{session.state.value}` — {agent_status_label(session.state)}")
    if session.final_score is not None:
        st.caption(f"Score ricalcolato dopo il recupero info: {session.final_score}")
    if session.pending_action:
        st.warning(
            f"Azione in attesa di approvazione umana: {session.pending_action.get('tool')}"
        )


def _render_agent(scored: ScoredLead, lead: Lead) -> None:
    st.subheader("Agente di risoluzione")
    if not scored.agent_triggered:
        st.caption(
            f"Nessun agente attivato: lead gestito dall'operatore (coda `{scored.queue}`). "
            "L'agente parte solo per lead ad alto valore con consenso al contatto."
        )
        return

    # One interactive session per (lead, consent, goal): the operator plays the lead,
    # typing replies the agent actually reacts to. The runner (hence its session store)
    # is kept in session_state so the conversation survives Streamlit reruns.
    key = f"{scored.lead_id}:{lead.consent}:{scored.agent_goal}"
    ag = st.session_state.get("agent")
    if ag is None or ag.get("key") != key:
        runner = AgentRunner()
        session = runner.start_session(scored, lead)
        if session is None:
            st.warning("La sessione agente non è stata creata.")
            return
        ag = {
            "key": key,
            "runner": runner,
            "lead_id": session.lead_id,
            "transcript": _action_items(session.actions),
            "seen": len(session.actions),
        }
        st.session_state["agent"] = ag

    runner = ag["runner"]
    session = runner.store.get(ag["lead_id"])
    if session is None:
        st.warning("La sessione agente non è più disponibile.")
        return

    st.caption(f"Obiettivo: {_goal_label(session.goal.value)} · avvio autonomo (API mock)")
    _render_transcript(ag["transcript"])

    if session.is_terminal:
        _render_outcome(session)
        return

    if session.state == AgentState.PENDING_APPROVAL:
        st.caption("L'agente ha predisposto un'azione: serve la tua approvazione.")
        c1, c2 = st.columns(2)
        if c1.button("Approva", type="primary", use_container_width=True):
            _advance(ag, human_approval(True))
            st.rerun()
        if c2.button("Rifiuta", use_container_width=True):
            _advance(ag, human_approval(False))
            st.rerun()
        return

    # Waiting for the customer's reply: the operator types as the customer.
    if st.button("Il cliente non risponde (timeout)"):
        _advance(ag, no_response())
        st.rerun()
    reply = st.chat_input("Rispondi come il cliente…")
    if reply:
        _advance(ag, user_reply(reply))
        st.rerun()


# --- main -------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Lead Scoring", layout="centered")
    _inject_css()
    _require_login()

    lead_dict = _current_lead()
    if lead_dict is None:
        _start_screen()
        return

    lead_dict = _sidebar_controls(lead_dict)
    try:
        lead = Lead.model_validate(lead_dict)
    except Exception as exc:  # noqa: BLE001 - surface any validation error to the UI
        st.error(f"Lead non valido: {exc}")
        return

    pipeline = get_pipeline()
    pipeline.reset_cache()  # re-score fresh so the consent toggle takes effect
    scored = pipeline.score_lead(lead, now=NOW)

    st.title("Lead Scoring")
    st.caption(f"Lead `{scored.lead_id}` · {lead.vehicle_interest or 'veicolo n/d'}")

    _render_scoring(scored)
    with st.expander("Payload lead", expanded=False):
        st.json(lead.model_dump(mode="json"))
    st.divider()
    _render_agent(scored, lead)


if __name__ == "__main__":
    main()
