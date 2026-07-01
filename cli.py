"""Operator review CLI for the lead-scoring pipeline (mock-first, no API key).

Modes:
* (default)          priority-sorted table of every lead;
* ``--detail <id>``  full explainability for one lead (validity, per-feature
  score contributions, motivation, personalization) plus, if the lead triggered
  the agent, the simulated resolution trajectory;
* ``--pending``      agent actions awaiting human approval (e.g. bookings,
  consent-gated messages), across all triggered leads.

Usage::

    py -3 cli.py
    py -3 cli.py --detail LEAD-0001
    py -3 cli.py --pending
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from src.action.suggestions import (
    QUEUE_ACTIVE,
    QUEUE_AGENT,
    QUEUE_DISCARDED,
    finalize_with_session,
)
from src.agent.runner import AgentRunner, user_reply
from src.config import get_settings
from src.models.lead import Lead
from src.models.output import ScoredLead
from src.pipeline import Pipeline
from src.scoring.scorer import top_contributions

NOW = datetime(2026, 6, 29, 12, 0, 0)


def _load_leads(path: Path) -> list[Lead]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read leads file {path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not isinstance(raw, list):
        print(f"Leads file {path} must contain a JSON array.", file=sys.stderr)
        raise SystemExit(2)
    return [Lead.model_validate(item) for item in raw]


def _score_all(leads: list[Lead], pipeline: Pipeline) -> list[ScoredLead]:
    scored = [pipeline.score_lead(lead, now=NOW) for lead in leads]
    scored.sort(key=lambda s: (s.priority, s.score), reverse=True)
    return scored


def _print_table(scored_leads: list[ScoredLead]) -> None:
    active = [s for s in scored_leads if s.queue == QUEUE_ACTIVE]
    agent_q = [s for s in scored_leads if s.queue == QUEUE_AGENT]
    discarded = [s for s in scored_leads if s.queue == QUEUE_DISCARDED]

    headers = ["lead_id", "category", "score", "priority", "conf", "prossima azione"]
    rows = [
        [s.lead_id, s.category, str(s.score), str(s.priority),
         "low" if s.low_confidence else "ok", s.next_best_action]
        for s in active
    ]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print("CODA ATTIVA (operatore, ordinata per priorità):")
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
    print(f"{len(active)} lead da chiamare.")

    if agent_q:
        print(f"\nGESTITI DALL'AGENTE (nessuna chiamata operatore): {len(agent_q)}")
        for s in agent_q:
            print(f"  {s.lead_id}  {s.agent_goal or '-'}  {s.next_best_action}")

    if discarded:
        print(f"\nSCARTATI in automatico (fuori dalla coda di chiamata): {len(discarded)}")
        for s in discarded:
            print(f"  {s.lead_id}  {s.motivation}")


def _print_detail(scored: ScoredLead, lead: Lead) -> None:
    # Resolve the async agent first (if triggered) so the view reflects the outcome.
    session = None
    if scored.agent_triggered:
        replies = [] if lead.consent is not True else [user_reply("Va bene sabato, confermo.")]
        session = AgentRunner().run_scripted(scored, lead, replies)
        if session is not None:
            scored = finalize_with_session(scored, session)

    v, f = scored.validity, scored.features
    print(f"Lead {scored.lead_id}")
    print(f"  category={scored.category}  score={scored.score}  priority={scored.priority}"
          f"  coda={scored.queue}  latency_ms={scored.latency_ms}  low_confidence={scored.low_confidence}")
    print(f"  motivazione: {scored.motivation}")
    print(f"  azione consigliata: {scored.recommended_action}")
    print(f"  prossima azione: {scored.next_best_action}")
    if scored.agent_status:
        print(f"  stato agente: {scored.agent_status}")

    print("\nGate di validità (deterministico):")
    print(f"  is_valid={v.is_valid}  failure_type={v.failure_type}  reasons={v.reasons}")

    print("\nEstrazione semantica (LLM, fonte={}):".format(f.extraction_source))
    print(f"  intent={f.intent_strength}  budget={f.budget_value_eur}  "
          f"specificity={f.vehicle_specificity}  trade_in={f.trade_in_present}  "
          f"availability={f.availability_mentioned}  looks_invalid={f.looks_invalid}")
    if f.missing_critical_fields:
        print(f"  campi mancanti: {f.missing_critical_fields}")

    print("\nContributi per-feature allo score (spiegabilità):")
    for name, val in sorted(scored.score_result.contributions.items(), key=lambda kv: kv[1], reverse=True):
        bar = "#" * int(round(val))
        print(f"  {name:20} {val:5.1f}  {bar}")

    p = scored.personalization
    if p.prior_leads_count:
        print(f"\nPersonalizzazione: ritorno={p.is_returning_customer} "
              f"duplicato={p.is_duplicate} contatti_precedenti={p.prior_leads_count}")

    if session is not None:
        print(f"\nAgente (obiettivo={scored.agent_goal}) — traiettoria simulata, "
              f"stato finale: {session.state.value}")
        for a in session.actions:
            print(f"   - {a.tool} [{a.status}] {a.reason}")


def _print_pending(scored_leads: list[ScoredLead], leads_by_id: dict[str, Lead]) -> None:
    runner = AgentRunner()
    rows: list[tuple[str, str, str]] = []
    for s in scored_leads:
        if not s.agent_triggered:
            continue
        lead = leads_by_id[s.lead_id]
        replies = [] if lead.consent is not True else [user_reply("Va bene sabato, confermo.")]
        session = runner.run_scripted(s, lead, replies)
        if not session:
            continue
        for a in session.actions:
            if a.status == "pending_approval":
                rows.append((s.lead_id, a.tool, a.reason))
    if not rows:
        print("Nessuna azione in attesa di approvazione umana.")
        return
    print(f"Azioni in attesa di approvazione umana ({len(rows)}):")
    for lead_id, tool, reason in rows:
        print(f"  {lead_id}  -> {tool}: {reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review lead scoring per il call center.")
    parser.add_argument("--data", type=Path, default=None)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--detail", metavar="LEAD_ID")
    group.add_argument("--pending", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    data_path = Path(args.data or settings.leads_mock_path)
    leads = _load_leads(data_path)
    leads_by_id = {ld.lead_id: ld for ld in leads}

    pipeline = Pipeline(settings)
    scored_leads = _score_all(leads, pipeline)

    if args.detail:
        match = next((s for s in scored_leads if s.lead_id == args.detail), None)
        if match is None:
            print(f"Nessun lead con id {args.detail!r} in {data_path}.", file=sys.stderr)
            return 1
        _print_detail(match, leads_by_id[match.lead_id])
    elif args.pending:
        _print_pending(scored_leads, leads_by_id)
    else:
        _print_table(scored_leads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
