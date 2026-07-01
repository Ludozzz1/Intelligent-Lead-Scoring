# Roadmap - dalla v1 deterministica all'apprendimento continuo

La **v1 e implementata** in questo take-home; il **futuro e documentato** (non
implementato). Ogni confine esterno e gia dietro un'interfaccia sostituibile
(`Protocol`/adapter), quindi ogni passo e una **sostituzione mirata, non un
refactor**.

## v1 - IMPLEMENTATO (cosa c'e oggi)

Gira **senza chiavi**, in modo deterministico e riproducibile.

### Zona 1 - Hot path (deterministico, <=2 min, una sola call LLM)

- **Gate strutturale** ([src/gate/validity.py](../src/gate/validity.py)): binario,
  NO LLM, fail-fast, blocklist esternalizzata
  ([config/blocklists.json](../config/blocklists.json)), invalidazione conservativa.
  Il consenso non e un check del gate.
- **Estrazione = la sola call LLM** ([src/extraction/](../src/extraction/)):
  mock-first via **fixture** ([data/mock_extractions.json](../data/mock_extractions.json)),
  PII redatte prima della chiamata, fallback deterministico `low_confidence` su
  timeout/errore. Nessun fallback keyword/regex.
- **Scoring** ([src/scoring/](../src/scoring/)): **una** funzione condivisa
  `build_feature_vector` (anti-skew) sulle 9 feature §5.3 + scorer lineare con
  **pesi naive** ([config/score_weights_naive.json](../config/score_weights_naive.json)),
  **contributi per-feature** esposti.
- **Categoria** ([src/categorization/bands.py](../src/categorization/bands.py)):
  bande da [config/category_thresholds.json](../config/category_thresholds.json);
  `invalid` dal gate o da `looks_invalid`.
- **Motivazione deterministica** ([src/motivation/](../src/motivation/)) — nessuna
  seconda call LLM.
- **Azione + trigger** ([src/action/decision.py](../src/action/decision.py)):
  `lead_valido`/`chiedere_info`/`scartare` + priority + (eventuale) goal agente.
- **Pipeline totale** ([src/pipeline/pipeline.py](../src/pipeline/pipeline.py)):
  stage guardati, idempotenza sul `lead_id`, fallback finale.

### Zona 2 - Agente (decoupled, event-driven)

- **Lead-Resolution Agent** come **state machine**
  ([src/agent/state_machine.py](../src/agent/state_machine.py)): stati TRIGGERED ->
  ... -> terminali (BOOKED / COMPLETED_INFO / HANDOFF_HUMAN /
  DISQUALIFIED_NO_RESPONSE / TERMINATED). Recupero info e negoziazione appuntamento
  = stessa macchina.
- **Tool mockati** ([src/agent/tools.py](../src/agent/tools.py)) + integrazioni mock
  (calendar, inventory, trade-in, channels); **guardrail e diritti di decisione**
  ([src/agent/guardrails.py](../src/agent/guardrails.py),
  [docs/decision_rights.md](decision_rights.md)); booking **human-approval** in v1;
  nessun `mark_invalid`.
- **Sessioni persistite** ([src/agent/session_store.py](../src/agent/session_store.py))
  e **runner** ([src/agent/runner.py](../src/agent/runner.py)) con **repliche
  simulate** per demo/test (l'evento reale arriverebbe async).

### Trasversale

- **FastAPI** ([src/main.py](../src/main.py)): `POST /score`, `/callback`,
  `/health`; **ingestion via coda mock** idempotente + **DLQ**; **callback mock** al
  monolite; integrazioni esterne mockate dietro `Protocol`.
- **Privacy** ([src/privacy.py](../src/privacy.py)): redazione PII + whitelist + token
  opachi + guardia `assert_no_raw_pii`.
- **Storico a runtime solo per dedup/personalizzazione**
  ([src/history.py](../src/history.py)).
- **Demo riproducibile** ([scripts/run_demo.py](../scripts/run_demo.py)), test,
  README, modello di costo, diagrammi.

## Futuro - DOCUMENTATO (non implementato)

### F1 - Training dei pesi appresi (calibrazione offline)

Pipeline **batch** (non a runtime) che apprende i pesi dallo storico e produce un
**artifact** consumato dallo scorer (dettaglio in [calibration.md](calibration.md)):

- **Label** dall'esito storico (`qualified`/`converted`).
- Per ogni lead storico: `ExtractedFeatures` -> **la stessa** `build_feature_vector`
  del runtime -> matrice `X`; label -> `y`. (E il punto anti-skew gia predisposto.)
- **Logistic regression** interpretabile su `(X, y)` -> coefficienti normalizzati =
  pesi -> `config/score_weights.json` (il loader lo userebbe **senza modifiche al
  codice**). Niente black-box: l'explainability e un KPI.
- **Soglie** ricalibrate dove il tasso di conversione per fascia scalina ->
  `config/category_thresholds.json`.
- **Backtest** modello vs pesi naive (baseline da battere), con caveat documentati
  (no leakage, selection bias, no overfitting su dati sintetici).

### F2 - Produzione event-driven reale su AWS

Sostituire i mock con i servizi reali, **logica invariata** (mappatura completa in
[architecture.md](architecture.md)):

- **Ingestion async reale**: l'app Java pubblica su **Amazon SQS (+ DLQ)**; il
  consumer di scoring elabora e riscrive lo score; retry+backoff con jitter,
  idempotenza "gratis" sul `lead_id`.
- **Coda/DB per l'agente**: sostituire `InMemorySessionStore` con un **store
  persistito** (Postgres) e un bus eventi reale che "sveglia" le sessioni sulle
  risposte in arrivo (oggi simulate dal runner).
- **LLM su provider EU**: adapter -> **Amazon Bedrock (EU)** / Azure OpenAI EU,
  modello Haiku-class (data-residency GDPR).
- **Callback reale + dashboard**: REST verso il backend **Java**; **push WebSocket**
  per riordinare in tempo reale la coda di priorita nella dashboard **Vue** (nessuna
  riscrittura frontend).
- **Storage + audit**: score/feature/sessioni su **Postgres (RDS/Aurora)**; storico
  di training su **S3/Redshift**; **log immutabile** delle decisioni automatiche
  (GDPR Art. 22).
- **Canali outbound reali**: WhatsApp/SMS/email via provider (Twilio, SES) dietro
  l'attuale `Channel` Protocol.

### F3 - Monitoring, drift e ricalibrazione continua

- **Monitoring online**: distribuzione di score/categorie, tasso fallback/timeout
  LLM, tasso DLQ, % PII redatta, latenze p50/p95/p99 vs SLA.
- **Drift detection** sulle feature e sulle conversioni per fascia; allarme quando
  la distribuzione si discosta dalla calibrazione.
- **Feedback loop**: gli esiti operatore + conversioni reali rientrano nello
  storico; un job periodico ri-allena i pesi (F1) e ri-taglia le soglie. E il
  meccanismo che lega lo storico al miglioramento continuo.

### F4 - Re-engagement e automazione outbound estesa (dietro consenso)

- **Re-engagement** dei lead raffreddati (alto rischio normativo): sempre **dietro
  consenso verificato**, piu template/canali, sequenze di nurturing per i `cold`,
  reminder per i `hot` — estensione naturale dell'agente, sotto i diritti di
  decisione gia definiti. **Fuori scope v1** (REFACTOR_SPEC §12).
- **Multi-agente** (solo se giustificato dai volumi futuri): citato come evoluzione,
  **non** prioritario — a questo scope il singolo agente deterministico e la scelta
  corretta.
