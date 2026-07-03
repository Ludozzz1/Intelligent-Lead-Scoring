"""Measure the REAL OpenAI spend of the scoring pipeline on a sample of leads.

Runs a sample of leads through the ACTUAL hot-path extraction (one LLM call each)
and, for the leads that trigger the agent, through the real agent planner, reading
token usage straight from ``response.usage``. It then scales the measured per-lead
cost to the daily volume and estimates the call-center savings.

Needs a live OpenAI backend: set ``OPENAI_API_KEY`` and ``LLM_MODE=openai`` and
install ``.[llm]``. Without it the script exits -- there is nothing real to measure.

PRICES BELOW ARE PLACEHOLDERS. Confirm the current EUR / 1M-token rates on
https://platform.openai.com/pricing and override with --price-* if they differ.
Token counts are measured exactly; only the euro conversion depends on the prices.

The measurement wraps the two adapters' clients at the instance level; ``src/`` is
not touched.

Run::

    .venv/Scripts/python scripts/estimate_cost.py
    .venv/Scripts/python scripts/estimate_cost.py --data data/leads_mock.json --limit 30
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from src.action.suggestions import QUEUE_AGENT, QUEUE_DISCARDED
from src.agent.runner import AgentRunner, human_approval, no_response, user_reply
from src.models.lead import Lead
from src.pipeline import get_pipeline

_REPO = Path(__file__).resolve().parent.parent
# Fixed reference time: keeps recency (and therefore the triggered set) stable.
NOW = datetime(2026, 6, 29, 12, 0, 0)

# --- assumptions (facts from the assignment) --------------------------------
DAILY_LEADS = 10_000
COST_PER_CALL_EUR = 4.0
DAYS_PER_MONTH = 30

# --- prices: EUR per 1M tokens. PLACEHOLDERS -- verify on the pricing page. ---
PRICE_EUR_PER_MTOK: dict[str, dict[str, float]] = {
    "gpt-5.4-mini": {"input": 0.15, "output": 0.60},   # extraction (hot path)
    "gpt-5.4": {"input": 2.50, "output": 10.00},       # agent planner (off-SLA)
}


def _cost_eur(model: str | None, prompt_tok: int, completion_tok: int) -> float:
    """EUR for a single call given its model and token counts (placeholder prices)."""
    price = PRICE_EUR_PER_MTOK.get(model or "") or PRICE_EUR_PER_MTOK["gpt-5.4-mini"]
    return prompt_tok / 1e6 * price["input"] + completion_tok / 1e6 * price["output"]


def _wrap_client(client, sink: list, tag: str) -> None:
    """Shadow ``client.chat.completions.create`` to record usage into ``sink``.

    Instance-level: reassigns the attribute on the client's ``completions`` object,
    so the real method still runs -- we only read ``response.usage`` on the way out.
    """
    original = client.chat.completions.create

    def metered(*args, **kwargs):
        resp = original(*args, **kwargs)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            sink.append((
                tag,
                kwargs.get("model"),
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
            ))
        return resp

    client.chat.completions.create = metered


def _scripted_replies(scored, lead) -> list:
    """Deterministic simulated replies (same shape as the demo) to drive the agent."""
    if lead.consent is not True:
        return []
    confirm = [user_reply("Va bene sabato, confermo il test drive."), human_approval()]
    if scored.agent_goal == "recover_info":
        return [
            user_reply("Il mio budget è 25000 euro, vorrei comprare entro un mese."),
            *confirm,
        ]
    return confirm


def _summarize(rows: list) -> dict:
    """Aggregate (tag, model, prompt, completion) rows into totals."""
    calls = len(rows)
    prompt_tok = sum(r[2] for r in rows)
    completion_tok = sum(r[3] for r in rows)
    eur = sum(_cost_eur(r[1], r[2], r[3]) for r in rows)
    return {
        "calls": calls,
        "prompt_tokens": prompt_tok,
        "completion_tokens": completion_tok,
        "total_tokens": prompt_tok + completion_tok,
        "eur": round(eur, 6),
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Measure real OpenAI cost on a lead sample.")
    p.add_argument("--data", default=str(_REPO / "data" / "leads_mock.json"),
                   help="JSON array of leads (default: data/leads_mock.json).")
    p.add_argument("--limit", type=int, default=30, help="Max leads to process.")
    p.add_argument("--no-agent", action="store_true",
                   help="Measure only the hot-path extraction, skip the agent.")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    pipeline = get_pipeline()
    if pipeline._llm.mode != "openai":
        print(
            "OpenAI backend not active -- nothing real to measure.\n"
            "Set OPENAI_API_KEY and LLM_MODE=openai in .env and install .[llm], then "
            "re-run.",
            file=sys.stderr,
        )
        sys.exit(1)
    pipeline.reset_cache()

    runner = AgentRunner()
    sink: list = []
    _wrap_client(pipeline._llm._client, sink, "extraction")
    if not args.no_agent and runner.tools.adapter.mode == "openai":
        _wrap_client(runner.tools.adapter._client, sink, "agent")

    raw_leads = json.loads(Path(args.data).read_text(encoding="utf-8"))[: args.limit]
    print(f"Sample: {len(raw_leads)} lead da {Path(args.data).name}\n")

    # -- Part A: hot-path extraction (one LLM call per valid, non-trivial lead) --
    scored_leads: list = []
    for raw in raw_leads:
        lead = Lead(**raw)
        scored = pipeline.score_lead(lead, now=NOW)
        scored_leads.append((scored, lead))

    # -- Part B: agent planner on the triggered subset --
    triggered = [(s, ld) for s, ld in scored_leads if s.agent_triggered]
    if not args.no_agent:
        for scored, lead in triggered:
            runner.run_scripted(scored, lead, _scripted_replies(scored, lead))

    # -- aggregate --
    extraction = _summarize([r for r in sink if r[0] == "extraction"])
    agent = _summarize([r for r in sink if r[0] == "agent"])

    n_leads = len(scored_leads)
    n_extract_calls = extraction["calls"]
    n_triggered = len(triggered)
    avoided = [s for s, _ in scored_leads if s.queue in (QUEUE_DISCARDED, QUEUE_AGENT)]

    extraction_ratio = n_extract_calls / n_leads if n_leads else 0.0
    triggered_ratio = n_triggered / n_leads if n_leads else 0.0
    avoided_ratio = len(avoided) / n_leads if n_leads else 0.0

    avg_extraction_eur = extraction["eur"] / n_extract_calls if n_extract_calls else 0.0
    avg_agent_eur = agent["eur"] / n_triggered if n_triggered else 0.0
    avg_agent_calls = agent["calls"] / n_triggered if n_triggered else 0.0

    # -- scale to the daily volume --
    daily_extraction_eur = DAILY_LEADS * extraction_ratio * avg_extraction_eur
    daily_agent_eur = DAILY_LEADS * triggered_ratio * avg_agent_eur
    daily_llm_eur = daily_extraction_eur + daily_agent_eur

    daily_baseline_eur = DAILY_LEADS * COST_PER_CALL_EUR
    daily_avoided_calls = DAILY_LEADS * avoided_ratio
    daily_savings_eur = daily_avoided_calls * COST_PER_CALL_EUR
    net_daily_eur = daily_savings_eur - daily_llm_eur
    roi = daily_savings_eur / daily_llm_eur if daily_llm_eur else 0.0

    # -- report --
    def line(label: str, value: str) -> None:
        print(f"  {label:<34} {value}")

    print("=" * 70)
    print("A) ESTRAZIONE (hot path, 1 call LLM per lead valido)")
    print("=" * 70)
    line("Lead processati", str(n_leads))
    line("Call LLM (lead che hanno estratto)", f"{n_extract_calls} ({extraction_ratio:.0%})")
    line("Token in / out (medi per call)",
         f"{extraction['prompt_tokens'] // max(1, n_extract_calls)} / "
         f"{extraction['completion_tokens'] // max(1, n_extract_calls)}")
    line("Costo medio per call", f"€ {avg_extraction_eur:.5f}")

    print("\n" + "=" * 70)
    print("B) AGENTE (planner LLM, off-SLA, solo lead triggerati)")
    print("=" * 70)
    line("Lead che triggerano l'agente", f"{n_triggered} ({triggered_ratio:.0%})")
    line("Call planner (medie per sessione)", f"{avg_agent_calls:.1f}")
    line("Costo medio per sessione", f"€ {avg_agent_eur:.5f}")

    print("\n" + "=" * 70)
    print(f"C) SCALING a {DAILY_LEADS:,} lead/giorno")
    print("=" * 70)
    line("Costo estrazione / giorno", f"€ {daily_extraction_eur:,.2f}")
    line("Costo agente / giorno", f"€ {daily_agent_eur:,.2f}")
    line("Costo LLM totale / giorno", f"€ {daily_llm_eur:,.2f}")
    line("Costo LLM totale / mese (x30)", f"€ {daily_llm_eur * DAYS_PER_MONTH:,.2f}")
    print("  " + "-" * 60)
    line("Baseline (tutti chiamati)", f"€ {daily_baseline_eur:,.2f} / giorno")
    line("Chiamate evitate", f"{daily_avoided_calls:,.0f} ({avoided_ratio:.0%})")
    line("Risparmio lordo / giorno", f"€ {daily_savings_eur:,.2f}")
    line("Risparmio netto / giorno", f"€ {net_daily_eur:,.2f}")
    line("ROI (risparmio / costo LLM)", f"{roi:,.0f}x")

    print("\nNOTE:")
    print("  - Prezzi PLACEHOLDER: verifica su platform.openai.com/pricing. I token")
    print("    sono misurati esatti; l'euro dipende dai prezzi.")
    print("  - Le percentuali (estrazione/trigger/evitate) vengono dal campione demo,")
    print("    NON dalla distribuzione di produzione: rilanciare su un campione storico")
    print("    rappresentativo per i ratio reali. Il costo per-call/per-sessione è invece")
    print("    una misura reale trasferibile.")

    report = {
        "sample": {"file": Path(args.data).name, "leads": n_leads},
        "prices_eur_per_mtok": PRICE_EUR_PER_MTOK,
        "prices_are_placeholders": True,
        "extraction": {
            **extraction,
            "ratio_leads_hitting_llm": round(extraction_ratio, 4),
            "avg_eur_per_call": round(avg_extraction_eur, 6),
        },
        "agent": {
            **agent,
            "triggered_leads": n_triggered,
            "triggered_ratio": round(triggered_ratio, 4),
            "avg_calls_per_session": round(avg_agent_calls, 3),
            "avg_eur_per_session": round(avg_agent_eur, 6),
        },
        "scaling_daily": {
            "leads": DAILY_LEADS,
            "extraction_eur": round(daily_extraction_eur, 2),
            "agent_eur": round(daily_agent_eur, 2),
            "llm_eur": round(daily_llm_eur, 2),
            "llm_eur_monthly": round(daily_llm_eur * DAYS_PER_MONTH, 2),
        },
        "savings_daily": {
            "baseline_eur": daily_baseline_eur,
            "avoided_ratio": round(avoided_ratio, 4),
            "avoided_calls": round(daily_avoided_calls, 1),
            "gross_savings_eur": round(daily_savings_eur, 2),
            "net_savings_eur": round(net_daily_eur, 2),
            "roi_x": round(roi, 1),
        },
    }
    out = _REPO / "deliverables" / "cost_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReport salvato in {out.relative_to(_REPO)}")


if __name__ == "__main__":
    main()
