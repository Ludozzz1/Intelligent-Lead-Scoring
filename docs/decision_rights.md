# Matrice dei diritti di decisione dell'agente (v1)

Riferimento: REFACTOR_SPEC §7.5. Definisce **cosa l'agente può fare in autonomia**
e cosa richiede un essere umano. Implementata in
[src/agent/guardrails.py](../src/agent/guardrails.py) (`DECISION_RIGHTS` + `enforce`)
e applicata dal loop in [src/agent/state_machine.py](../src/agent/state_machine.py).

Il loop è guidato da un **planner** ([planner.py](../src/agent/planner.py)):
deterministico in `llm_mode=mock` (default), LLM-driven altrove. Vale l'invariante
**"l'LLM propone, il deterministico dispone"**: ogni decisione del planner passa da
`enforce()`, l'unico chokepoint che applica questa matrice + allow-list + consenso.

| Azione                                   | Autorità in v1            | Tool / chiave |
|------------------------------------------|---------------------------|------|
| Chiedere info mancanti                    | **auto, solo con consenso** | `request_missing_info` |
| **Proporre uno slot (negoziazione autonoma)** | **auto, solo con consenso** | `propose_slots` |
| Inviare un messaggio / asset             | **auto, solo con consenso** | `send_message` / `send_asset` |
| Acquisire il consenso (double opt-in)    | **auto** (è il messaggio che *crea* la base) | `capture_consent` |
| Stima permuta / simulazione finanziamento | **auto** (range indicativo) | `estimate_trade_in` / `simulate_financing` |
| Verifica inventario / alternative        | **auto**                  | `check_inventory` / `recommend_alternatives` |
| Verifica disponibilità calendario        | **auto**                  | `check_availability` |
| Re-analisi della risposta utente         | **auto**                  | `re_extract` |
| Follow-up programmato (ladder)           | **auto**                  | `schedule_followup` |
| Writeback CRM dell'esito                 | **auto**                  | `update_crm` |
| Trasferimento caldo a operatore          | **auto**                  | `warm_transfer_to_operator` / `escalate_to_human` |
| **Confermare la prenotazione**           | **human-approval (v1)**   | `book_appointment` → stage → `PENDING_APPROVAL` |
| **Disqualificare un lead per qualità**   | **mai all'agente**        | resta il gate deterministico |

## Principi

- **Consent gating** (§7.5, §9): nessun messaggio outbound senza una base di
  consenso verificata. Il consenso è ora valutato **a monte**, nel routing
  ([decide_action](../src/action/decision.py)): un lead senza consenso va all'operatore
  invece di attivare un goal — così `enforce()` resta una **difesa in profondità** che
  scatta raramente (se manca il consenso a runtime: azione registrata `pending_approval`
  → `HANDOFF_HUMAN`).
- **Re-scoring async non-disqualificante** (§7.2): dopo un `recover_info` l'agente
  ri-estrae, fonde e **ri-score** la risposta (riusando `build_feature_vector`, nessuna
  call LLM extra); un lead promosso prosegue al booking, uno ancora debole va in
  **nurturing** (`send_asset` → `NURTURED`) o all'operatore — **mai** in `invalid`.
- **Human-in-the-loop sull'azione costosa/irreversibile**: la **prenotazione** è un
  **gate vero**. L'agente **non** prenota in autonomia: `enforce()` mette l'azione in
  *stage* (`AgentSession.pending_action`), la registra `pending_approval` e la
  sessione si ferma in `PENDING_APPROVAL`. Solo un evento `HUMAN_APPROVAL`
  dell'operatore esegue davvero `book_appointment` → `BOOKED` (con
  `approved=False` → handoff). La **negoziazione degli slot resta autonoma** (solo
  consenso + budget): l'umano sanziona unicamente l'azione irreversibile.
- **L'agente non invalida mai per qualità**: può chiudere solo per **non-risposta**
  (`DISQUALIFIED_NO_RESPONSE`) o per **handoff**. L'unica autorità che marca un
  lead `invalid` è il gate deterministico (§5.1) + il `looks_invalid` dell'LLM.
- **Fallback sempre disponibile**: errore di un tool o budget di passi superato →
  `HANDOFF_HUMAN`, mai loop infiniti (§7.4).

## Guardrail (stop conditions, §7.4)

| Guardrail            | Sorgente                         | Esito |
|----------------------|----------------------------------|-------|
| `max_turns`          | `settings.agent_max_turns` (6)   | → handoff |
| `max_messages`       | `settings.agent_max_messages` (4)| → handoff |
| `max_followups`      | `settings.agent_max_followups` (2)| → handoff (oltre la ladder) |
| `max_llm_calls`      | `settings.agent_max_llm_calls` (8)| → handoff (tetto di costo planner) |
| `response_timeout`   | evento `NO_RESPONSE_TIMEOUT`     | → `DISQUALIFIED_NO_RESPONSE` |
| Incertezza / fuori scope | risposta ambigua/rifiuto     | → handoff |
| Fallimento tool       | eccezione del tool              | → handoff |
| **Fallimento planner LLM** | timeout/errore/output invalido | **degrade a planner deterministico** (mai blocco) |
