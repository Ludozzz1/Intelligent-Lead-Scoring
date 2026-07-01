"""Streamlit review UI per la pipeline di lead scoring (mock-first, no API key).

Riproduce la chiamata che il monolite Java fa al servizio: carica un lead, ne
esegue lo scoring in-process (``pipeline.score_lead``) e — se il lead attiva
l'agente — ne mostra la traiettoria event-driven con repliche del lead SIMULATE,
rendendo visibili tutti i passaggi e le decisioni di entrambe le zone.

Accesso a password (env ``APP_PASSWORD``, con default demo). Il consenso al
contatto non è incluso nei lead reali: questo strumento lo imposta automaticamente
a ON, con un toggle per testare anche il ramo senza consenso (gestione operatore).

Run::

    ./.venv/Scripts/python.exe -m streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import streamlit as st

from src.action.suggestions import agent_status_label, finalize_with_session
from src.agent.runner import AgentRunner, human_approval, no_response, user_reply
from src.config import get_settings
from src.models.agent import AgentAction, AgentEvent, AgentEventType
from src.models.lead import Lead
from src.models.output import ScoredLead
from src.pipeline import get_pipeline
from src.scoring.scorer import top_contributions

# Fixed evaluation clock: keeps recency (and therefore scores) reproducible,
# aligned with cli.py / scripts/run_demo.py.
NOW = datetime(2026, 6, 29, 12, 0, 0)
DEMO_PASSWORD = "autoxy-demo"


# --- auth -------------------------------------------------------------------

def _require_login() -> None:
    """Gate the app behind a shared password (env APP_PASSWORD, default demo)."""
    if st.session_state.get("authed"):
        return
    expected = os.environ.get("APP_PASSWORD", DEMO_PASSWORD)
    st.title("Lead Scoring")
    st.caption("Accesso riservato al call center.")
    pw = st.text_input("Password", type="password")
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


def _pick_lead() -> dict | None:
    """Sidebar controls: choose a bundled sample or upload a lead JSON file."""
    st.sidebar.header("Lead in ingresso")
    source = st.sidebar.radio("Sorgente", ("Lead campione", "Carica file JSON"), index=0)

    if source == "Lead campione":
        samples = _load_samples()
        if not samples:
            st.sidebar.error("Nessun lead campione in data/leads_mock.json.")
            return None
        key = st.sidebar.selectbox("Seleziona lead", list(samples))
        return samples.get(key)

    up = st.sidebar.file_uploader("File lead (.json)", type=["json"])
    if up is None:
        return None
    try:
        data = json.loads(up.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        st.sidebar.error(f"JSON non valido: {exc}")
        return None
    if isinstance(data, list):
        idx = {str(r.get("lead_id") or f"#{i}"): r for i, r in enumerate(data)}
        key = st.sidebar.selectbox("Lead nel file", list(idx))
        return idx.get(key)
    if isinstance(data, dict):
        return data
    st.sidebar.error("Il file deve contenere un oggetto lead o un array di lead.")
    return None


def _apply_consent(lead_dict: dict) -> dict:
    """Consent toggle: auto-ON when the lead omits it (real feeds do not send it)."""
    missing = lead_dict.get("consent") is None
    default = True if missing else bool(lead_dict.get("consent"))
    consent = st.sidebar.toggle("Consenso al contatto", value=default)
    if missing:
        st.sidebar.caption(
            "Consenso non presente nel lead: impostato automaticamente da questo "
            "strumento. Spegnilo per simulare il ramo senza consenso (operatore)."
        )
    return {**lead_dict, "consent": consent}


# --- scoring view -----------------------------------------------------------

def _render_scoring(scored: ScoredLead) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Score", scored.score)
    c2.metric("Categoria", scored.category)
    c3.metric("Priorità", scored.priority)
    c4.metric("Coda", scored.queue)
    if scored.low_confidence:
        st.warning("Estrazione a bassa confidenza: segnali semantici limitati.")

    with st.expander("1 · Gate di validità (deterministico, no LLM)", expanded=True):
        v = scored.validity
        st.write(f"Valido: **{v.is_valid}** · tipo failure: `{v.failure_type}`")
        st.write("Motivi:", v.reasons)
        if v.missing_fields:
            st.write("Campi mancanti:", v.missing_fields)

    with st.expander("2 · Estrazione semantica (unica call LLM)", expanded=True):
        f = scored.features
        st.write(f"Fonte: `{f.extraction_source}` · confidenza: {f.extraction_confidence:.2f}")
        st.table([
            {"segnale": "intento d'acquisto", "valore": str(f.intent_strength)},
            {"segnale": "budget", "valore": str(f.budget_value_eur) if f.budget_present else "—"},
            {"segnale": "specificità veicolo", "valore": str(f.vehicle_specificity)},
            {"segnale": "permuta", "valore": str(f.trade_in_vehicle or f.trade_in_present)},
            {"segnale": "disponibilità", "valore": str(f.availability_mentioned)},
            {"segnale": "sentiment", "valore": str(f.sentiment)},
        ])
        if f.missing_critical_fields:
            st.write("Campi critici mancanti:", f.missing_critical_fields)
        if f.rationale_signals:
            st.caption(f.rationale_signals)

    with st.expander("3 · Contributi per-feature allo score (spiegabilità)", expanded=True):
        sr = scored.score_result
        st.caption(f"Pesi: {sr.weights_source}")
        top = top_contributions(sr, k=3)
        if top:
            st.write("Driver principali: " + ", ".join(f"{n} ({v:.1f})" for n, v in top))
        ranked = sorted(sr.contributions.items(), key=lambda kv: kv[1], reverse=True)
        mx = max((v for _, v in ranked), default=1.0) or 1.0
        for name, val in ranked:
            st.write(f"{name}: {val:.1f}")
            st.progress(min(1.0, max(0.0, val / mx)))

    st.subheader("Motivazione")
    st.info(scored.motivation)

    st.subheader("Azione")
    st.write(f"Azione consigliata: **{scored.recommended_action}**")
    st.write(f"Prossima azione operatore: {scored.next_best_action}")
    trig = "sì" if scored.agent_triggered else "no"
    goal = f" · obiettivo: `{scored.agent_goal}`" if scored.agent_goal else ""
    st.write(f"Agente attivato: **{trig}**{goal}")


# --- agent view -------------------------------------------------------------

def _scripted_events(scored: ScoredLead, responsive: bool) -> list[AgentEvent]:
    """Deterministic simulated events driving the session to a terminal state.

    Mirrors scripts/run_demo.py: a recover-info goal first supplies the missing
    info (the agent re-scores and gets promoted), then a slot is confirmed and the
    staged booking is approved by the operator.
    """
    if not responsive:
        return [no_response()]
    confirm = [user_reply("Va bene sabato, confermo il test drive."), human_approval()]
    if scored.agent_goal == "recover_info":
        return [
            user_reply("Il mio budget è 25000 euro, vorrei comprare entro un mese."),
            *confirm,
        ]
    return confirm


def _event_display(event: AgentEvent) -> tuple[str, str]:
    if event.type == AgentEventType.USER_REPLY:
        return "Replica lead (simulato)", event.text or ""
    if event.type == AgentEventType.NO_RESPONSE_TIMEOUT:
        return "Timeout (simulato)", "il lead non ha risposto entro la finestra"
    if event.type == AgentEventType.HUMAN_APPROVAL:
        verdict = "approva" if event.approved else "rifiuta"
        return "Operatore (simulato)", f"{verdict} l'azione predisposta"
    return "Evento", event.type.value


_STATUS_LABEL = {
    "executed": "eseguito",
    "pending_approval": "in attesa di approvazione",
    "skipped": "saltato",
    "failed": "fallito",
}


def _render_actions(actions: list[AgentAction]) -> None:
    if not actions:
        st.caption("nessuna nuova azione")
        return
    for a in actions:
        st.markdown(f"- **{a.tool}** · {_STATUS_LABEL.get(a.status, a.status)}")
        if a.reason:
            st.caption(a.reason)
        if a.args or a.result:
            with st.expander(f"dettaglio · {a.tool}"):
                if a.args:
                    st.write("args:", a.args)
                if a.result:
                    st.write("result:", a.result)


def _render_agent(scored: ScoredLead, lead: Lead) -> None:
    st.header("Agente di risoluzione")
    if not scored.agent_triggered:
        st.info(
            f"Nessun agente attivato: il lead è gestito dall'operatore (coda "
            f"`{scored.queue}`). L'agente parte solo per lead ad alto valore con "
            "consenso al contatto."
        )
        return

    responsive = st.radio(
        "Esito simulato della conversazione",
        ("Il lead risponde e conferma", "Il lead non risponde"),
        horizontal=True,
    ) == "Il lead risponde e conferma"

    runner = AgentRunner()
    session = runner.start_session(scored, lead)
    if session is None:
        st.warning("La sessione agente non è stata creata.")
        return

    st.subheader("Kickoff dell'agente (autonomo, mock API)")
    st.caption(f"Obiettivo: {session.goal.value} · stato iniziale: {session.state.value}")
    _render_actions(session.actions)
    seen = len(session.actions)

    for event in _scripted_events(scored, responsive):
        if session.is_terminal:
            break
        kind, text = _event_display(event)
        st.markdown(f"**{kind}:** {text}")
        updated = runner.resume_on_reply(session.lead_id, event)
        if updated is None:
            break
        session = updated
        _render_actions(session.actions[seen:])
        seen = len(session.actions)

    st.subheader("Esito")
    st.write(f"Stato finale: **{session.state.value}** — {agent_status_label(session.state)}")
    if session.final_score is not None:
        st.write(f"Score ricalcolato dopo il recupero info: {session.final_score}")
    if session.pending_action:
        st.warning(
            f"Azione in attesa di approvazione umana: {session.pending_action.get('tool')}"
        )

    final_view = finalize_with_session(scored, session)
    st.markdown("**Vista operatore riallineata all'esito**")
    st.write(f"Coda: `{final_view.queue}` · azione: `{final_view.recommended_action}`")
    st.write(f"Prossima azione: {final_view.next_best_action}")


# --- main -------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Lead Scoring", layout="wide")
    _require_login()

    st.title("Lead Scoring — valutazione lead")
    st.caption(
        "Carica un lead per riprodurre la chiamata del monolite al servizio: "
        "scoring hot-path e, se previsto, l'agente di risoluzione."
    )

    lead_dict = _pick_lead()
    if lead_dict is None:
        st.info("Seleziona un lead campione o carica un file JSON per iniziare.")
        return

    lead_dict = _apply_consent(lead_dict)
    try:
        lead = Lead.model_validate(lead_dict)
    except Exception as exc:  # noqa: BLE001 - surface any validation error to the UI
        st.error(f"Lead non valido: {exc}")
        return

    with st.expander("Lead in ingresso (payload)"):
        st.json(lead.model_dump(mode="json"))

    pipeline = get_pipeline()
    pipeline.reset_cache()  # re-score fresh so the consent toggle takes effect
    scored = pipeline.score_lead(lead, now=NOW)

    st.header("Scoring (hot path)")
    _render_scoring(scored)
    st.divider()
    _render_agent(scored, lead)


if __name__ == "__main__":
    main()
