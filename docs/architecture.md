# Architettura tecnica

Documento di dettaglio modulo per modulo dell'architettura **a due zone**
(REFACTOR_SPEC §3). Per la narrativa d'insieme e i trade-off vedi
[progettazione.md](progettazione.md); per i diagrammi
[diagrams/flow_e2e.txt](diagrams/flow_e2e.txt) e
[diagrams/scoring_axes.txt](diagrams/scoring_axes.txt).

## 1. Vista a due zone

```
            ZONA 1 - HOT PATH (deterministico, <=2 min SLA, UNA sola call LLM)
  lead --> [gate] --invalid--> categoria=invalid, scartare (STOP, niente LLM)
             | valido
             v
        [extraction]  <- LA SOLA call LLM (mock-first)  --timeout--> fallback low_confidence
             v
        [build_feature_vector]  ->  [scorer lineare]  (score 0-100 + contributi)
             v
        [categoria]  ->  [motivazione deterministica]  ->  [azione + trigger]
             |
   ==========|=========================== confine SLA ============================
             | (solo hot/warm con interazione aperta)
             v
            ZONA 2 - AGENTE (asincrono, event-driven, decoupled, NON nello SLA)
        Lead-Resolution Agent = state machine persistita, tool mockati, guardrail.
        Obiettivo: stato terminale (booked / completed_info / handoff / no-response).
```

Lo **SLA dei 2 minuti riguarda lo score**, non l'interazione: l'agente vive *dopo*
lo scoring e su orizzonte lungo (l'utente puo rispondere dopo minuti/ore o mai).
La **calibrazione dei pesi appresi e OFFLINE** (non implementata, vedi
[calibration.md](calibration.md)); i **pesi naive** sono il fallback attivo.

## 2. Modello dei dati ([src/models/](../src/models/))

Modelli Pydantic v2 (`extra="ignore"`, tipi `str | None`), default sicuri ovunque
(i feed reali sono parziali: la pipeline non crasha su input incompleti):

- [src/models/lead.py](../src/models/lead.py) — `Lead` (input grezzo, tutti i campi
  opzionali, `consent: bool | None`) e **`ExtractedFeatures`** (i segnali semantici
  prodotti dall'unica call LLM: `intent_strength`, `budget_present/_value_eur`,
  `vehicle_specificity`, `trade_in_present`, `availability_mentioned`, `sentiment`,
  `urgency_signals`, `missing_critical_fields`, `looks_invalid`,
  `extraction_confidence`, `rationale_signals`, `extraction_source`). La property
  `low_confidence` e True su `fallback`/`skipped`/`none` o quando
  `extraction_confidence < 0.5`.
- [src/models/features.py](../src/models/features.py) — `FeatureVector` (valori
  normalizzati nominati + lista delle feature semantiche) e `ScoreResult` (`score`,
  `contributions` per-feature, `weights_source` in `{naive, learned}`, `confidence`,
  `low_confidence`).
- [src/models/scoring.py](../src/models/scoring.py) — `ValidityResult`
  (`is_valid`, `failure_type` in `{none, invalid, incomplete}`, `reasons`,
  `missing_fields`) e `Personalization` (dedup/cliente di ritorno).
- [src/models/agent.py](../src/models/agent.py) — `AgentState`, `AgentGoal`,
  `AgentEvent(Type)`, `AgentAction`, `AgentSession`.
- [src/models/output.py](../src/models/output.py) — `ScoredLead`, il contratto di
  output completo. **Rimossi** rispetto al legacy: l'asse `risk` (`RiskResult`) e il
  `QualityBreakdown` a 4 bucket; la confidenza e ora `low_confidence`
  dall'estrazione. **Aggiunti** per l'operatore (§5.6 spec, deterministici):
  `next_best_action` (cosa fare ora, vocabolario chiuso — il *perché* resta in
  `motivation`), `queue` (`attiva|agente|scartato`) e `agent_status`.
- [src/action/suggestions.py](../src/action/suggestions.py) — vocabolario chiuso del
  `next_best_action` (mirror del tool belt dell'agente), routing `queue` (gli `invalid`
  escono dalla coda attiva) e `finalize_with_session` (riallinea il lead all'esito
  dell'agente quando la sessione si risolve).

## 3. Configurazione ([src/config.py](../src/config.py))

`Settings(BaseSettings)` (pydantic-settings, `.env`). Espone **solo knob di
runtime**: modalita/timeout LLM (`llm_mode`, `llm_timeout_s=8.0`), path di dati e
artifact, `dedup_window_days`, guardrail agente (`agent_max_turns=6`,
`agent_max_messages=4`). I **pesi e le soglie NON sono settings**: sono artifact
JSON in `config/` (vedi §6), cosi lo score e calibrabile senza toccare codice o env.
`model_post_init` risolve i path relativi alla radice repo, a prescindere dalla
working directory. `get_settings()` e `lru_cache`d.

## 4. Pipeline — Zona 1 ([src/pipeline/pipeline.py](../src/pipeline/pipeline.py))

`Pipeline` costruisce una sola volta i singleton (`HistoryService`, `LLMAdapter`) e
valuta un lead per volta con `score_lead`. Ordine degli stage:

```
(1) lead_id          stabile (lead.lead_id oppure hash phone_key|email_key|created_at)
    idempotenza       cache hit -> ScoredLead in cache (is_duplicate=True)
(2) gate             evaluate_validity (regole, NO LLM): valido | invalid
(3) personalizzaz.   history.personalize: dedup / cliente di ritorno (no scoring)
(4) extraction       extract_features: gated + PII-redacted; LA SOLA call LLM o fallback
(5) feature vector   build_feature_vector (condivisa) -> compute_score (lineare, pesi naive)
(6) categoria        categorize: bande hot/warm/cold; invalid da gate o looks_invalid
(7) motivazione      build_motivation deterministica (rationale_signals + top contributi)
(8) azione           decide_action: lead_valido|chiedere_info|nurturing|scartare + agent goal + priority
(9) suggerimento     classify_queue (attiva|agente|scartato) + build_next_best_action (vocab chiuso)
    assemblaggio      stamp processed_at + latency_ms -> ScoredLead
```

**`score_lead` e una funzione totale.** Ogni stage e avvolto in `_safe(...)` che
logga e ritorna un default sicuro invece di sollevare; se l'intera flow fallisce,
`_fallback_invalid` produce uno `ScoredLead` `invalid`/`low_confidence`. Cosi
un'eccezione imprevista non viola mai lo SLA ne propaga un 5xx. **L'agente NON gira
qui**: `score_lead` imposta solo `agent_triggered`/`agent_goal` e ritorna; la zona 2
e decoupled (§9).

## 5. Gate di validita ([src/gate/validity.py](../src/gate/validity.py))

`evaluate_validity(lead, history, settings) -> ValidityResult`. **Strutturale,
binario, NO LLM, fail-fast** (REFACTOR_SPEC §5.1): scarta a costo zero i lead
inutilizzabili *prima* di spendere la call LLM. Check:

- **Telefono**: normalizzazione IT; `_is_bogus_phone` (algoritmico: tutte cifre
  uguali / sequenze ascendenti-discendenti) -> evidenza fake; mobile `3xxxxxxxx(x)`
  o fisso `0...` -> canale raggiungibile.
- **Email**: formato + dominio non nella **blocklist esternalizzata**
  (`disposable_email_domains` da [config/blocklists.json](../config/blocklists.json),
  caricata da [src/scoring/weights.py](../src/scoring/weights.py)).
- **Decisione**: `invalid` se c'e **evidenza fake** (telefono bogus o email
  usa-e-getta) **oppure** nessun canale raggiungibile; altrimenti **valido**.
- **Dedup**: riportato in `reasons`, **non invalida mai** (un cliente di ritorno e
  alto valore).

Il **consenso NON e un check del gate**: gestisce l'invio outbound dell'agente
(§7.5), non la scorabilita. Il **giudizio semantico** (spam/gibberish/fuori area)
NON e qui: e `ExtractedFeatures.looks_invalid` dell'LLM, applicato dopo il gate.
**Invalidazione conservativa**: un dato ambiguo non invalida; il false-invalid
(buttare un compratore vero) e l'errore piu costoso.

## 6. Scoring ([src/scoring/](../src/scoring/))

### 6.1 Feature-building condivisa (anti-skew)

[src/scoring/feature_vector.py](../src/scoring/feature_vector.py) —
`build_feature_vector(features, lead, now) -> FeatureVector`. **L'unica** funzione
che trasforma `ExtractedFeatures` + campi strutturati in un vettore normalizzato
nominato. E il punto chiave **anti-skew**: la stessa funzione e riusabile verbatim
dalla (futura) pipeline di training offline sui lead storici, cosi modello e runtime
scorano sullo **stesso vettore**, mai sul testo grezzo. Solo le **9 feature §5.3**:

| Tipo | Feature | Normalizzazione |
|---|---|---|
| semantica (LLM) | `intent_strength` | high 1.0 / medium 0.6 / low 0.2 |
| semantica (LLM) | `budget_present` | 1.0 / 0.0 |
| semantica (LLM) | `vehicle_specificity` | specific 1.0 / generic 0.5 / none 0.0 |
| semantica (LLM) | `trade_in_present` | 1.0 / 0.0 |
| semantica (LLM) | `availability` | 1.0 / 0.0 |
| semantica (LLM) | `sentiment` | positive 1.0 / neutral 0.5 / negative 0.0 |
| strutturale | `reachability` | mobile 1.0 / fisso o email 0.6 / 0.0 |
| strutturale | `recency` | decadimento lineare su 30 giorni; 0.5 se ignota |
| strutturale | `geo_match` | in-catchment 1.0 / adiacente 0.5 / lontano 0.1 / 0.3 ignoto |

**Niente storico, niente catalogo veicoli** a runtime. `geo_match` usa il catchment
del dealer da [config/dealer_catchment.json](../config/dealer_catchment.json).

### 6.2 Scorer lineare

[src/scoring/scorer.py](../src/scoring/scorer.py) — `compute_score(vector, features,
settings) -> ScoreResult`. Prodotto scalare `somma(valore * peso)`, clampato 0-100;
espone i **`contributions` per-feature** (spiegabilita/dashboard). L'LLM non e mai
in questo path: un'estrazione degradata cambia solo i *valori* delle feature
semantiche (e `low_confidence`), mai l'aritmetica. `top_contributions(...)` serve a
motivazione e CLI.

### 6.3 Pesi e artifact

[src/scoring/weights.py](../src/scoring/weights.py) — loader cache-ati con fallback:

- `load_weights`: preferisce l'artifact **appreso** `config/score_weights.json`
  (NON incluso: training fuori scope); altrimenti i **pesi naive**
  [config/score_weights_naive.json](../config/score_weights_naive.json) (fallback
  attivo, sommano a 100). Ritorna `(weights, source)`.
- `load_thresholds`: bande + cutoff di automazione da
  [config/category_thresholds.json](../config/category_thresholds.json)
  (hot 72 / warm 45 / cold 25; `warm_high` 62).
- `load_catchment`, `load_blocklists`: catchment geo e blocklist del gate.

Tutti degradano a default built-in se un file manca/e illeggibile.

## 7. Categoria ([src/categorization/bands.py](../src/categorization/bands.py))

`categorize(score, is_valid, looks_invalid, settings) -> str`. `invalid` se gate
fallito **o** `looks_invalid` dell'LLM; altrimenti `hot`/`warm`/`cold` per bande
sullo score. `invalid` **non e una banda** (anti-pattern §11): viene dal gate o
dall'LLM. Le soglie sono lette dall'artifact, mai hardcoded a intuito.

## 8. Motivazione e azione

- **Motivazione** ([src/motivation/motivation.py](../src/motivation/motivation.py)):
  `build_motivation(...)` e **deterministica** (REFACTOR_SPEC §5.5) —
  `rationale_signals` dell'estrazione + le top `contributions` con etichette
  italiane per l'operatore. **Nessuna seconda call LLM** (rimosso l'"explainer"
  legacy). Aggiunge `[estrazione a bassa confidenza]` quando serve.
- **Azione** ([src/action/decision.py](../src/action/decision.py)):
  `decide_action(...)` -> `ActionDecision(recommended_action, agent_goal, priority)`.
  Routing **allineato al valore + consenso** (`route_complete`, sorgente unica riusata
  anche dal re-scoring dell'agente): automazione ristretta ai lead ad alto valore
  (`hot` e `warm >= warm_high`). `invalid` -> `scartare`. **Una sola regola** attiva
  l'agente: consenso **e** `score >= warm_high` (gli `hot` sono sempre sopra). Se
  mancano info -> `chiedere_info` + goal `recover_info` (recupera, ri-score e prenota,
  §7.2); se completo -> `lead_valido` + goal `negotiate_appointment` (booking proattivo).
  Warm medio/basso, `cold` (label `nurturing`) o senza consenso -> operatore, **mai**
  agente. Il
  **consenso e valutato a monte** (niente trigger che collassa in handoff). `priority`
  (0-100) e la banda della categoria + boost in-banda da score e cliente di ritorno ->
  ordine della coda call center.

## 9. Zona 2 — Lead-Resolution Agent ([src/agent/](../src/agent/))

Un **singolo** agente, **decoupled** dallo SLA, **event-driven**. La diff col legacy:
l'agente NON gira dentro `score_lead` (sincrono); `score_lead` imposta solo un
trigger, e il runner avanza una sessione persistita sugli eventi.

- **Planner** ([src/agent/planner.py](../src/agent/planner.py)): la policy che
  **propone** la prossima azione (`PlannerDecision`). Due implementazioni dietro lo
  stesso protocollo: `DeterministicPlanner` (**default in `llm_mode=mock`**: traduce
  1:1 le traiettorie legacy, keyword matching, comportamento invariato) e `LLMPlanner`
  (**native tool-calling** OpenAI via `LLMAdapter.complete_tool_call` — campo `tools`
  + `tool_choice="required"`, **fuori SLA**; tool-defs/prompt in
  [agent_prompts.py](../src/agent/agent_prompts.py)). L'`LLMPlanner` **traduce** il
  tool call nativo in un `PlannerDecision` (i 3 control tool `wait_for_user`/`complete`/
  `handoff` coprono le azioni non-tool; l'FSM transition è derivata in codice). Su
  errore/timeout/nessun tool call il loop **degrada** al deterministico (mai blocco).
  `enforce()` resta il chokepoint invariato. Invariante: **l'LLM propone, il
  deterministico dispone**.
- **Loop controller** ([src/agent/state_machine.py](../src/agent/state_machine.py)):
  `advance(session, event, tools, settings, *, planner=None)` per ogni passo chiede al
  planner una decisione, la passa da `enforce()`, esegue il tool, registra audit e
  transita — fino a wait/terminale/stage. Stati: `TRIGGERED`, `AWAITING_USER_REPLY`,
  `EVALUATING_REPLY`, `PROPOSING_SLOT`, `AWAITING_CONFIRMATION`, il non-terminale
  `PENDING_APPROVAL`, terminali `BOOKED`, `COMPLETED_INFO`, `HANDOFF_HUMAN`,
  `DISQUALIFIED_NO_RESPONSE`, `TERMINATED`. Recupero info e negoziazione sono **la
  stessa macchina** su traiettorie diverse.
- **Re-scoring async dopo recupero info** (`planner._eval_recover` + `_rescore`): alla
  risposta dell'utente l'agente `re_extract`-a, **fonde** le feature
  (`scoring.feature_vector.merge_features`) e **ri-calcola** score/categoria riusando la
  **stessa** `build_feature_vector`/`semantic_values` sul vettore strutturale cachato nella
  sessione (`base_features`/`base_vector`) — **nessuno skew, nessuna call LLM extra**. Poi
  ri-instrada con la stessa `route_complete`: lead promosso a booking-worthy → prosegue al
  booking nello stesso wake; `warm` medio o `cold` → operatore (`COMPLETED_INFO`); ancora
  incompleto → continua a chiedere (bounded) o consegna all'operatore. Nessun `cold` viene
  automatizzato (§7.1). Tutto **fuori SLA**.
- **Gate di approvazione del booking**: `book_appointment` è `human_approval`.
  `enforce()` lo **mette in stage** (`session.pending_action`), lo registra
  `pending_approval` e la sessione si ferma in `PENDING_APPROVAL`; un evento
  `HUMAN_APPROVAL` (operatore) lo esegue → `BOOKED`. La **negoziazione degli slot
  resta autonoma** (solo consenso + budget).
- **Tool mockati** ([src/agent/tools.py](../src/agent/tools.py)): oltre ai base
  (`re_extract`, `check_inventory`, `check_availability`, `estimate_trade_in`,
  `send_message`, `book_appointment`, `escalate_to_human`), il toolset automotive:
  `simulate_financing`, `recommend_alternatives`, `send_asset`, `capture_consent`,
  `schedule_followup`, `update_crm`, `warm_transfer_to_operator`. Appoggiati alle
  integrazioni mock ([calendar](../src/integrations/calendar.py),
  [inventory](../src/integrations/inventory.py),
  [trade_in](../src/integrations/trade_in.py),
  [channels](../src/integrations/channels.py),
  [financing](../src/integrations/financing.py),
  [scheduler](../src/integrations/scheduler.py),
  [monolith_callback](../src/integrations/monolith_callback.py)). Destinatari **token
  opachi** (mai PII). **Nessun `mark_invalid`**: l'agente non disqualifica per qualità.
- **Guardrail + enforcement**
  ([src/agent/guardrails.py](../src/agent/guardrails.py)): `enforce()` è l'unico
  chokepoint — applica `DECISION_RIGHTS` (auto / auto_if_consent / human_approval /
  never) + allow-list + `consent_ok` + budget, restituendo una `EnforcedDecision`
  (call_tool / stage / wait_user / complete / handoff). `limit_breached` copre
  passi/messaggi/follow-up/call-LLM. La matrice è in
  [decision_rights.md](decision_rights.md).
- **Persistenza** ([src/agent/session_store.py](../src/agent/session_store.py)):
  `SessionStore` Protocol con `InMemorySessionStore` (default) e `FileSessionStore`
  (JSON). Lo stato vive in uno store -> l'agente si "sveglia" sugli eventi, non e
  request-response.
- **Runner** ([src/agent/runner.py](../src/agent/runner.py)): `start_session`,
  `resume_on_reply` (evento async reale), `run_scripted` (repliche **simulate** per
  demo/test). In prod l'evento arriva dalla coda; qui e simulato.

## 10. Privacy / PII ([src/privacy.py](../src/privacy.py))

Implementazione **canonica**, applicata **prima** di ogni call LLM:

- `redact_message`: email -> `[EMAIL]`, telefoni -> `[PHONE]`, nomi auto-introdotti
  -> `[NAME]` (ordine: email, poi telefoni, poi nomi).
- `safe_fields_for_llm`: **whitelist** non-PII (channel, platform, campaign,
  vehicle_interest, city) + `zip_prefix` (prime 3 cifre, area non locatore preciso).
- `phone_key` / `email_key`: chiavi dedup SHA-256 canoniche (combaciano con quelle
  in `data/leads_history.json`).
- `assert_no_raw_pii`: guardia difensiva che solleva se un telefono/email grezzo
  sopravvive alla redazione, **prima** di passare il testo all'adapter.

**Residency EU**: la sola call LLM e mappabile su Amazon Bedrock (regione EU) per la
data-residency GDPR (§11). Lo score e deterministico e auditabile (contributi +
motivazione) -> pronto al GDPR Art. 22.

## 11. Estrazione — la sola call LLM ([src/extraction/](../src/extraction/))

- [src/extraction/extractor.py](../src/extraction/extractor.py) —
  `extract_features(lead, validity, adapter)`. Ordine: **(1) GATE** — lead invalid o
  message banale (<3 char) -> `skipped`, niente LLM; **(2) REDACT** PII +
  `assert_no_raw_pii`; **(3) UNA** call all'adapter; su `LLMError`/qualsiasi
  eccezione -> `_fallback()` (solo-strutturale, `low_confidence`). **Non esiste un
  fallback keyword/regex "NLP"**: l'estrazione semantica e compito dell'LLM (mock =
  fixture).
- [src/extraction/llm.py](../src/extraction/llm.py) — `LLMAdapter`, unico punto di
  contatto con un LLM reale, un solo metodo `extract` (mai assegna uno score). Tre
  modalita: **MOCK** (default, **fixture map** message->ExtractedFeatures da
  [data/mock_extractions.json](../data/mock_extractions.json); message non in fixture
  -> default low-confidence), **OPENAI** (solo con `llm_mode=openai` + key +
  pacchetto, JSON-schema strict, `temperature=0`, timeout da settings; in prod
  -> Bedrock EU), **auto-degrade** (circuit breaker: a `_CB_THRESHOLD=3` fallimenti
  consecutivi passa a MOCK).

## 12. Integrazioni mockate ([src/integrations/](../src/integrations/))

Confini esterni dietro `Protocol`, sostituibili senza toccare la logica:

- [queue.py](../src/integrations/queue.py): `Queue` Protocol +
  `InMemoryQueue`/`FileQueue` + `DeadLetterQueue` + `consume_all` (drain async; un
  handler che solleva manda l'item in **DLQ** e il drain prosegue). Stand-in di SQS+DLQ.
- [monolith_callback.py](../src/integrations/monolith_callback.py):
  `MockMonolithCallback.send_score` (ack di consegna, payload minimale non-PII).
- [channels.py](../src/integrations/channels.py),
  [calendar.py](../src/integrations/calendar.py),
  [inventory.py](../src/integrations/inventory.py),
  [trade_in.py](../src/integrations/trade_in.py),
  [financing.py](../src/integrations/financing.py) (ammortamento deterministico),
  [scheduler.py](../src/integrations/scheduler.py) (follow-up): tool dell'agente, mock
  deterministici (id/slot/range/rate derivati da hash), failure simulabili per testare
  guardrail e handoff. **Nessun catalogo auto**: l'inventario e un tool, non una
  feature di scoring. `monolith_callback` espone anche `send_agent_outcome` (writeback
  CRM dell'esito agente, non-PII).

## 13. Storico ([src/history.py](../src/history.py))

`HistoryService` carica `data/leads_history.json` una volta e a runtime fa **solo**
due cose (REFACTOR_SPEC §11 vieta lo storico come input di scoring): **dedup**
(`find_duplicate`) e **personalizzazione** (`personalize` -> `Personalization`).
Le label di esito nel file restano, ma sono consumate **solo OFFLINE** dalla
calibrazione documentata ([calibration.md](calibration.md)), mai qui. **Rimosso** il
prior storico per-strato che il legacy usava nello scoring (source_quality/risk).
Robusto: file mancante/corrotto -> servizio vuoto, nessuna eccezione.

## 14. Entrypoint ([src/main.py](../src/main.py), [scripts/run_demo.py](../scripts/run_demo.py))

- `main.py`: FastAPI thin — `POST /score` (sincrono, ritorna lo `ScoredLead`),
  `POST /callback` (ricevitore mock del monolite), `GET /health` (modalita LLM
  effettiva, storico caricato, versione).
- `scripts/run_demo.py`: demo end-to-end a due zone — i 26 lead mock via
  `InMemoryQueue` -> `consume_all` (DLQ) -> `Pipeline.score_lead` -> callback; poi il
  runner guida le sessioni dell'agente con repliche **simulate** fino agli stati
  terminali.

---

## Gestione degli errori, fallback e idempotenza

| Meccanismo | Dove | Comportamento |
|---|---|---|
| **Fallback deterministico LLM** | [extractor.py](../src/extraction/extractor.py) | Timeout/errore/JSON invalido -> `ExtractedFeatures(extraction_source="fallback")`, `low_confidence`. Possibile *perche* l'LLM non e nello score: lo SLA non dipende mai dall'LLM. |
| **Timeout duro LLM** | [config.py](../src/config.py) (`llm_timeout_s=8.0`) | La sola call ha timeout 8 s, ben dentro lo SLA di 2 minuti. |
| **Circuit breaker** | [llm.py](../src/extraction/llm.py) (`_CB_THRESHOLD=3`) | Dopo 3 fallimenti OpenAI consecutivi l'adapter passa a MOCK; il contatore si azzera su un successo. |
| **Degrade planner LLM** | [state_machine.py](../src/agent/state_machine.py) | `LLMPlanner` su errore/timeout/output invalido (`LLMError`) → il loop passa a `DeterministicPlanner` e prosegue: l'agente non si blocca mai. |
| **DLQ (poison message)** | [queue.py](../src/integrations/queue.py) | Un handler che solleva manda l'item in DLQ con la ragione; il drain prosegue (un lead malformato non blocca il batch). |
| **Idempotenza / dedup** | [pipeline.py](../src/pipeline/pipeline.py) (`_processed`) + [history.py](../src/history.py) | `lead_id` derivato deterministicamente; ri-valutare lo stesso lead torna la cache con `is_duplicate=True`, senza ri-triggerare l'agente. |
| **Guard per-stage** | [pipeline.py](../src/pipeline/pipeline.py) (`_safe`) | Ogni stage degrada a un default sicuro; `_fallback_invalid` come rete finale. La pipeline non solleva mai. |
| **Errori tool agente** | [state_machine.py](../src/agent/state_machine.py) (`_safe`/`_handoff`) | Un tool che fallisce -> `HANDOFF_HUMAN`, mai stato inconsistente o loop infinito. |
| **Budget passi/messaggi** | [guardrails.py](../src/agent/guardrails.py) | `agent_max_turns`/`agent_max_messages` superati -> handoff. |
| **No-response** | [state_machine.py](../src/agent/state_machine.py) | Evento `NO_RESPONSE_TIMEOUT` -> `DISQUALIFIED_NO_RESPONSE`. |
| **Invalidazione conservativa** | [validity.py](../src/gate/validity.py) | In incertezza il lead resta valido; l'`invalid` richiede evidenza strutturale positiva. |
| **Redazione PII difensiva** | [privacy.py](../src/privacy.py) (`assert_no_raw_pii`) | Solleva se PII grezze sopravvivono alla redazione prima dell'LLM. |
| **Import difensivo** | [llm.py](../src/extraction/llm.py) | `openai` importato con `try/except ImportError`: il core gira anche senza, in MOCK. |

## Mappatura sulla produzione AWS

| Confine (mock take-home) | Produzione (target documentato) |
|---|---|
| `InMemoryQueue`/`FileQueue` + `DeadLetterQueue` | **Amazon SQS + DLQ**; consumer su EKS con retry+backoff (idempotente sul `lead_id`) |
| `LLMAdapter` mock-first / OpenAI dietro flag | **Amazon Bedrock (EU)** o Azure OpenAI EU, modello Haiku-class (data-residency GDPR) |
| `MockMonolithCallback` | REST verso il **backend Java**; push WebSocket per riordinare la coda nella dashboard **Vue** (nessuna riscrittura frontend) |
| JSON su file (`data/*.json`, session store) | **Postgres (RDS/Aurora)** per score/feature/sessioni; **S3/Redshift** per lo storico di training; **audit log immutabile** delle decisioni automatiche (GDPR) |
| `MockChannel` / `MockCalendar` / `MockInventory` / `MockTradeIn` | provider reali (Twilio, SES), scheduling del dealer, DMS/inventory, servizio stima permuta |
| Deploy locale | container su **EKS/Kubernetes**, segreti via secret manager, HPA leggero |
