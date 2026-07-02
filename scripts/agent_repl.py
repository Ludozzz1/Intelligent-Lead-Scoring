"""REPL interattivo per guidare UNA sessione agente con risposte libere (LLM reale).

A differenza di run_demo/cli/streamlit (che iniettano 2 risposte scriptate), qui
digiti risposte arbitrarie del lead e i verdetti dell'operatore: cosi si testa
davvero il ragionamento del planner LLM. Riusa l'``AgentRunner`` esistente; nessuna
modifica al core.

Perche' l'agente sia guidato dall'LLM serve ``llm_mode=openai`` + key nel .env; in
mock il planner e' deterministico. Il REPL segnala quando rileva il degrade
silenzioso (planner LLM fallito -> fallback deterministico).

Uso::

    py -3 scripts/agent_repl.py --lead LEAD-0002
    py -3 scripts/agent_repl.py --data data/leads_adversarial.json --lead ADV-0001

Comandi durante il loop:
    <testo>      -> risposta del lead (user_reply)
    /approva     -> operatore approva l'azione predisposta (es. booking)
    /rifiuta     -> operatore rifiuta l'azione predisposta
    /silenzio    -> il lead non risponde entro la finestra (timeout)
    /esci        -> termina la sessione
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from src.action.suggestions import finalize_with_session
from src.agent.runner import AgentRunner, human_approval, no_response, user_reply
from src.config import get_settings
from src.models.agent import AgentAction, AgentSession
from src.models.lead import Lead
from src.pipeline import Pipeline

NOW = datetime(2026, 6, 29, 12, 0, 0)
_OUTBOUND = frozenset({"send_message", "send_asset", "capture_consent"})
_LLM_TAG = "pianificato dall'LLM"


def _safe_stdout() -> None:
    """Best-effort UTF-8 console so Italian accents / symbols render on Windows."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _load_leads(path: Path) -> dict[str, Lead]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {item["lead_id"]: Lead(**item) for item in raw}


def _print_actions(new: list[AgentAction]) -> int:
    """Print a batch of agent actions; return how many were LLM-planned."""
    llm_planned = 0
    for a in new:
        tag = ""
        if a.reason.startswith(_LLM_TAG):
            llm_planned += 1
            tag = " [LLM]"
        line = f"   - {a.tool} [{a.status}]{tag}"
        text = (a.args.get("text") or "").strip() if a.args else ""
        if a.tool in _OUTBOUND and text:
            line += f'  «{text}»'
        elif a.reason:
            line += f"  — {a.reason}"
        print(line)
    return llm_planned


def _status(session: AgentSession, llm_planned: int) -> None:
    print(f"   -> stato={session.state.value} · turni={session.turns} "
          f"· llm_calls={session.llm_calls} · azioni_LLM_batch={llm_planned}")
    if session.state.value == "PENDING_APPROVAL" and session.pending_action:
        print(f"   [attesa] approvazione umana per '{session.pending_action.get('tool')}'"
              " -> /approva oppure /rifiuta")


def _event_from(line: str):
    cmd = line.strip()
    if cmd in ("/esci", "/quit", "/q"):
        return "quit"
    if cmd == "/approva":
        return human_approval(True)
    if cmd == "/rifiuta":
        return human_approval(False)
    if cmd == "/silenzio":
        return no_response()
    return user_reply(cmd)


def _finish(scored, session: AgentSession, mode: str) -> None:
    print(f"\n=== FINE · stato_finale={session.state.value} ===")
    if session.final_score is not None:
        print(f"score ricalcolato dopo il recupero info: {session.final_score}")
    view = finalize_with_session(scored, session)
    print(f"vista operatore: coda={view.queue} · azione={view.recommended_action} "
          f"· {view.next_best_action}")
    # Degrade check a livello di SESSIONE (accurato): le azioni di controllo
    # (handoff/complete) non portano il tag [LLM], quindi un controllo per-batch
    # darebbe falsi positivi. Un planner LLM funzionante marca almeno un tool di
    # dominio nel kickoff; zero tag [LLM] in tutta la sessione = planner degradato.
    llm_tagged = sum(1 for a in session.actions if a.reason.startswith(_LLM_TAG))
    if mode == "openai" and session.actions and llm_tagged == 0:
        print("[!] possibile degrade: nessuna azione 'pianificato dall'LLM' "
              "nell'intera sessione -> il planner LLM potrebbe essere fallito "
              "(fallback deterministico). Controlla i model ID.")
    print("\nAudit completo della sessione:")
    for a in session.actions:
        print(f"  {a.tool}[{a.status}] {a.reason}")


def main(argv: list[str] | None = None) -> int:
    _safe_stdout()
    p = argparse.ArgumentParser(
        description="REPL interattivo per l'agente di lead-resolution (LLM reale).")
    p.add_argument("--lead", metavar="LEAD_ID", help="id del lead da guidare")
    p.add_argument("--data", type=Path, default=None,
                   help="file JSON dei lead (default: leads_mock.json)")
    args = p.parse_args(argv)

    settings = get_settings()
    data_path = Path(args.data or settings.leads_mock_path)
    try:
        leads = _load_leads(data_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Impossibile leggere {data_path}: {exc}", file=sys.stderr)
        return 2

    if not args.lead:
        print("Specifica --lead <id>. Disponibili:", ", ".join(leads))
        return 2
    lead = leads.get(args.lead)
    if lead is None:
        print(f"Lead {args.lead!r} non trovato in {data_path}.", file=sys.stderr)
        return 1

    mode = settings.llm_mode
    print(f"llm_mode={mode} · agent_model={settings.openai_agent_model} "
          f"· extract_model={settings.openai_model}")
    if mode != "openai":
        print("[!] llm_mode != openai: stai testando il planner DETERMINISTICO "
              "(mock), NON l'LLM.")

    pipeline = Pipeline(settings)
    scored = pipeline.score_lead(lead, now=NOW)
    print(f"\nLead {scored.lead_id}: score={scored.score} cat={scored.category} "
          f"fonte_estrazione={scored.features.extraction_source} "
          f"agent_goal={scored.agent_goal or '-'}")
    if not scored.agent_triggered:
        print(f"Agente NON attivato (coda={scored.queue}, "
              f"azione={scored.recommended_action}). "
              "Serve un lead hot/warm-high con consenso al contatto. Fine.")
        return 0

    runner = AgentRunner(settings=settings)
    session = runner.start_session(scored, lead)
    if session is None:
        print("Sessione agente non creata.", file=sys.stderr)
        return 1

    print(f"\n=== Sessione avviata · obiettivo={session.goal.value} ===")
    seen = 0
    llm = _print_actions(session.actions[seen:])
    seen = len(session.actions)
    _status(session, llm)

    while not session.is_terminal:
        try:
            line = input("\ntu> ")
        except EOFError:
            print("\n(EOF) chiudo.")
            break
        event = _event_from(line)
        if event == "quit":
            break
        session = runner.resume_on_reply(session.lead_id, event) or session
        llm = _print_actions(session.actions[seen:])
        seen = len(session.actions)
        _status(session, llm)

    _finish(scored, session, mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
