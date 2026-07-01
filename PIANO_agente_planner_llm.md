# Piano — Upgrade agente a planner LLM + toolset esteso

## Context

Oggi la "zona agentica" è una **macchina a stati deterministica**: le transizioni
sono `if/elif` cablati e l'interpretazione delle risposte è keyword matching
(`_CONFIRM_WORDS/_COUNTER_WORDS/_REFUSAL_WORDS` in
[state_machine.py:29-31](src/agent/state_machine.py#L29-L31)); l'unica call LLM
dell'agente (`re_extract`) viene **calcolata ma mai usata** per decidere
([state_machine.py:135-148](src/agent/state_machine.py#L135-L148)).

Obiettivo: portare il **loop di ragionamento sotto controllo di un LLM** che
orchestra l'interazione (tool-calling), **senza perdere** determinismo, sicurezza
e mock-first. È architetturalmente lecito perché la zona agentica vive **fuori
dallo SLA di 2 min** (async, event-driven): più call LLM lì non toccano il budget
di latenza dell'hot path. Non si viola "LLM-as-judge" (quello vieta l'LLM nello
*scoring*, qui è orchestrazione conversazionale). Si aggiungono inoltre 8 tool
mirati allo use case automotive (finanziamento, permuta, alternative, asset,
CRM writeback, warm transfer, follow-up ladder, consenso GDPR).

## Invarianti da preservare (non negoziabili)

- **L'LLM propone, il deterministico dispone**: ogni azione passa da `enforce()`.
- **Mock-first**: test senza API key → in `llm_mode="mock"` il planner è la logica
  deterministica attuale (comportamento invariato, test esistenti restano verdi).
- **PII**: `redact_message` + token opachi (`to_token`) su ogni call LLM.
- **Scoring deterministico**: l'agente non ri-scora né invalida (resta il gate).
- **Budget come ceiling**: turni/messaggi/follow-up/call LLM → niente loop runaway.
- **Tutto fuori SLA**: nessun impatto sul percorso di scoring.

## Architettura (approccio scelto)

1. **Planner astratto** — nuovo `src/agent/planner.py`:
   - `PlannerDecision` (pydantic): `action ∈ {call_tool, wait_user, complete, handoff}`,
     `tool`, `args`, `next_state`, `rationale`.
   - `Planner` protocol: `.next_action(session, event, tools, settings) -> PlannerDecision`.
   - `DeterministicPlanner`: la logica delle traiettorie attuali estratta da
     `state_machine.py` (`_start_recover_info`, `_start_negotiation`, `_on_reply_*`)
     → **default mock-first**, equivalente al comportamento odierno.
   - `LLMPlanner`: tool-calling reale via `LLMAdapter`, prompt/schema in nuovo
     `src/agent/agent_prompts.py` (stesso pattern di
     [prompts.py::EXTRACTION_JSON_SCHEMA](src/extraction/prompts.py#L54)).

2. **Loop generalizzato** — `advance()` in
   [state_machine.py:34](src/agent/state_machine.py#L34) diventa un controller:
   sceglie il planner in base a `settings.llm_mode` → `while` non
   (wait_user | terminale | budget_breached): `decision = planner.next_action()`
   → `enforce()` → esegue via `AgentTools` → `_record()` → aggiorna stato.
   **Firma invariata** → `runner.py` e la persistenza restano intatti.

3. **Enforcement** — in `src/agent/guardrails.py`, nuovo
   `enforce(decision, session, settings) -> EnforcedDecision`: applica
   `DECISION_RIGHTS` (auto / auto_if_consent / human_approval / never) +
   **allow-list dei tool** + `consent_ok` + budget. `book_appointment` resta
   `human_approval` → `pending_approval`; `disqualify_for_quality` resta `never`.

4. **Adapter** — estendere
   [LLMAdapter](src/extraction/llm.py#L69) con un metodo riusabile
   `complete_json(system, messages, schema)` (mock deterministico + path OpenAI con
   `response_format` json_schema). Stesso circuit-breaker dell'estrazione: su
   errore/timeout/bad-output → `LLMError` → il loop degrada a `DeterministicPlanner`.

## Nuovi tool (in `AgentTools` + integrazioni mock)

| Tool | File | Decision-right | Note |
|------|------|----------------|------|
| `simulate_financing(prezzo, anticipo, permuta)` | nuovo `src/integrations/financing.py` (`MockFinancing`, ammortamento deterministico) | `auto` | Rata mensile; leva su "budget 35k" |
| `estimate_trade_in` | già in [tools.py:54](src/agent/tools.py#L54) | `auto` | **Agganciare** (oggi mai chiamato) + aggiungere a `DECISION_RIGHTS` |
| `recommend_alternatives(veicolo, budget)` | estende `MockInventory` / `tools.py` | `auto` | Se modello out-of-stock → SUV simile in catchment |
| `send_asset(veicolo, tipo)` | template su `MockChannel` | `auto_if_consent` | Scheda/listino/configuratore |
| `update_crm(lead_id, esito, note)` | estende `monolith_callback` (`send_agent_outcome`) | `auto` | Oggi il callback riporta solo lo score |
| `warm_transfer_to_operator(lead_id, contesto)` | `tools.py` (handoff ricco) | `auto` | Contesto completo all'operatore |
| `schedule_followup(lead_id, quando)` | nuovo `src/integrations/scheduler.py` (`MockScheduler`) | `auto` | Cambia NO_RESPONSE: prima follow-up ladder, poi disqualify |
| `capture_consent(lead_id)` | template double opt-in su `MockChannel` | `auto` | Se consenso assente: tenta capture prima dell'handoff |

## Modelli & config

- `src/models/agent.py`: + contatori `followups_sent: int = 0`,
  `consent_requested: bool = False`, `llm_calls: int = 0`. Stati attuali
  sufficienti (riuso `AWAITING_USER_REPLY`); `PlannerDecision` vive in `planner.py`.
- `src/config.py`: + `agent_max_followups: int = 2`, +
  `agent_max_llm_calls: int = 8` (ceiling di costo per sessione).

## File toccati

- **Nuovi**: `src/agent/planner.py`, `src/agent/agent_prompts.py`,
  `src/integrations/financing.py`, `src/integrations/scheduler.py`,
  `tests/test_agent_planner.py`.
- **Modificati**: `src/agent/state_machine.py` (loop), `src/agent/guardrails.py`
  (`enforce` + nuovi `DECISION_RIGHTS`), `src/agent/tools.py` (nuovi tool),
  `src/extraction/llm.py` (`complete_json`), `src/models/agent.py` (contatori),
  `src/config.py` (knobs), `src/integrations/monolith_callback.py`
  (`send_agent_outcome`), `docs/architecture.md`, `docs/decision_rights.md`,
  `docs/cost_model.md`, `REFACTOR_SPEC.md` §7 (documentare planner + invariante
  "LLM propone / deterministico dispone" + nuovi tool).
- **Riuso (non riscrivere)**: `AgentTools` come tool belt,
  `_record`/`_safe`/`_handoff` (audit + handoff su tool error),
  `consent_ok`/`limit_breached`, il pattern `EXTRACTION_JSON_SCHEMA`,
  `MockChannel`/`MockCalendar`/`MockInventory`/`MockTradeIn`, `FileSessionStore`.

## Gestione errori (KPI)

- Tool error → `_safe` → handoff (invariato).
- **LLM planner** error/timeout/output invalido → degrade a `DeterministicPlanner`
  (circuit-breaker come l'estrazione) → il loop non si blocca mai; se anche il
  fallback non sa procedere → handoff `low_confidence`.
- Budget breach (turni/messaggi/follow-up/call LLM) → handoff.

## Costi & privacy (KPI per la live)

- Costo solo per i lead *triggered* (frazione del volume) e fuori SLA. Stimare
  costo/sessione ≈ `call_LLM × turni`; aggiornare `docs/cost_model.md`. Tetto con
  `agent_max_llm_calls`.
- Privacy: redazione + token opachi su ogni call del planner; nessun PII nei prompt
  (riusa `redact_message` / `safe_fields_for_llm`).

## Verifica

```bash
# 1) I test esistenti restano verdi (mock = DeterministicPlanner = comportamento attuale)
./.venv/Scripts/python.exe -m pytest tests/test_agent.py -v

# 2) Nuovi test: loop planner (stub LLMPlanner con decisioni scriptate),
#    enforce() che blocca (consenso / human_approval / allow-list / budget),
#    nuovi tool, follow-up ladder, capture_consent
./.venv/Scripts/python.exe -m pytest tests/test_agent_planner.py -v

# 3) Demo end-to-end (mock) invariata; opzionale stampare il rationale del planner
./.venv/Scripts/python.exe scripts/run_demo.py

# 4) Suite completa
./.venv/Scripts/python.exe -m pytest
```

Prova real-mode (opzionale, fuori test): `LLM_MODE=openai` + `OPENAI_API_KEY`,
verificando che su errore/timeout il loop degradi al planner deterministico.
