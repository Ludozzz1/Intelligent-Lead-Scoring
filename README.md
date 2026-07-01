# Lead Scoring Intelligente

Microservizio Python che valuta i lead in ingresso di un call center automotive
(~10.000 lead/giorno) e li prioritizza, assegnando a ciascuno **score 0-100**,
**categoria** (`hot` / `warm` / `cold` / `invalid`), **motivazione**, **azione
consigliata** (`lead_valido` / `chiedere_info` / `nurturing` / `scartare`) e una
**prossima azione** operativa (`next_best_action`, vocabolario chiuso) con il bucket
di **coda** (`attiva` / `agente` / `scartato`); quando serve, avvia un **agente di
risoluzione** che porta il lead a uno stato terminale e **riallinea** poi il lead
all'esito (es. `chiedere_info` → `lead_valido`, oppure prenotato/handoff).

Doppio fine di business: dare agli operatori una **priorità di chiamata** con un
suggerimento azionabile, e **ridurre i costi** — gli `invalid` escono in automatico
dalla coda di chiamata e i lead di valore con consenso sono auto-gestiti dall'agente.

## Architettura a due zone

**Zona 1 — Hot path** (deterministico, ≤2 min, **una sola call LLM**):

```
lead → [gate] → [extraction: UNICA call LLM] → [scoring lineare] → [categoria]
              (strutturale, no LLM)   ↓ timeout → fallback deterministico (low_confidence)
              → [motivazione deterministica] → [azione] → (trigger zona agentica)
```

- **Gate** ([src/gate/validity.py](src/gate/validity.py)): validazione **strutturale**,
  binaria, **nessun LLM**, con blocklist esternalizzata. Il consenso non è un check
  del gate (è dell'agente). Il giudizio semantico (spam/gibberish) è dell'LLM.
- **Estrazione** ([src/extraction/](src/extraction/)): **la sola call LLM** del hot
  path. Mock-first via **fixture** ([data/mock_extractions.json](data/mock_extractions.json)),
  PII redatte prima della chiamata. Produce `ExtractedFeatures` (§5.2). L'estrazione
  semantica è compito dell'LLM — **niente liste/keyword hardcoded**.
- **Scoring** ([src/scoring/](src/scoring/)): **una** funzione condivisa
  `build_feature_vector` (anti-skew training/runtime) sulle sole feature §5.3
  (segnali LLM + raggiungibilità + recency + geo). Score = combinazione lineare con
  **pesi naive** ([config/score_weights_naive.json](config/score_weights_naive.json)),
  con **contributi per-feature** esposti (spiegabilità).
- **Categoria** ([src/categorization/](src/categorization/)): bande da
  [config/category_thresholds.json](config/category_thresholds.json). `invalid` viene
  dal gate o dal `looks_invalid` dell'LLM, non è una banda.
- **Motivazione** ([src/motivation/](src/motivation/)): **deterministica** da
  `rationale_signals` + contributi. **Nessuna seconda call LLM.**

**Zona 2 — Agente** ([src/agent/](src/agent/)): un singolo **Lead-Resolution Agent**
event-driven, **fuori dallo SLA**. Automazione **ristretta ai lead ad alto valore**
(`hot` e `warm ≥ warm_high`): completo → `negotiate_appointment` (booking proattivo);
incompleto **con estrazione ricca** (`recovery_worthy`: copertura ≥ `recovery_coverage_min`,
non la banda) → `recover_info` (poi **ri-score** e ri-instrada); `cold`, warm medio o senza
consenso → operatore (i `cold` **mai** in automazione). Il loop è guidato da un **planner** — deterministico in mock
(default, comportamento riproducibile, zero token) o **LLM** off-SLA con degrade —
ma ogni azione passa da `enforce()` (**"l'LLM propone, il deterministico dispone"**):
tool mockati, guardrail e diritti di decisione (vedi
[docs/decision_rights.md](docs/decision_rights.md)). La negoziazione degli slot è
autonoma; la **prenotazione è un gate human-approval** (staged → `PENDING_APPROVAL`
→ evento `HUMAN_APPROVAL` → `BOOKED`). L'agente non disqualifica mai per qualità.

> **Codice in inglese, documentazione in italiano.** Dettagli in
> [docs/architecture.md](docs/architecture.md), [docs/progettazione.md](docs/progettazione.md),
> [docs/cost_model.md](docs/cost_model.md), [docs/calibration.md](docs/calibration.md),
> [docs/decision_rights.md](docs/decision_rights.md), [docs/roadmap.md](docs/roadmap.md).

## Prerequisiti

- **Python ≥ 3.11** (testato con CPython 3.13).
- Nessuna chiave: demo, CLI, API e test girano **completamente offline** con mock
  deterministici. OpenAI è opzionale (vedi sotto).

## Setup

```bash
# 1. virtualenv
py -3 -m venv .venv          # Windows; su Linux/macOS: python3 -m venv .venv

# 2. installa il pacchetto in editable con gli extra di sviluppo
.venv/Scripts/python -m pip install -e ".[dev]"
```

Su Linux/macOS l'interprete del venv è `.venv/bin/python`. Extra opzionali:
`.[llm]` (client OpenAI), `.[cli]` (rich).

## Demo end-to-end (senza chiavi)

Pubblica i lead mock su una coda in-memory (stand-in di SQS), li valuta (hot path,
una sola call LLM) e li riscrive al monolite mock; poi guida le **sessioni
dell'agente** con repliche simulate fino agli stati terminali
(booked / completed_info / handoff / disqualified_no_response). Stampa infine la
**vista operatore** in tre code — `attiva` (con `next_best_action`), `agente`
(auto-gestiti) e `scartati` — con la stima di chiamate evitate.

```bash
.venv/Scripts/python scripts/run_demo.py
```

## Review CLI per il call center

```bash
.venv/Scripts/python cli.py                    # coda priorizzata
.venv/Scripts/python cli.py --detail LEAD-0001 # explainability + contributi + traiettoria agente
.venv/Scripts/python cli.py --pending          # azioni in attesa di approvazione umana
```

## Frontend Streamlit (review UI)

Interfaccia web minimale che **riproduce la chiamata del monolite** al servizio:
carica un lead (campione o file JSON), esegue lo scoring in-process e — se il lead
attiva l'agente — ne mostra la **traiettoria** con repliche del lead **simulate**,
rendendo visibili tutti i passaggi e le decisioni di entrambe le zone.

```bash
.venv/Scripts/python -m pip install -e ".[ui]"   # aggiunge streamlit
.venv/Scripts/python -m streamlit run streamlit_app.py
```

- **Accesso a password**: default demo `autoxy-demo`, sovrascrivibile con la env var
  `APP_PASSWORD` (nessun segreto committato).
- **Consenso**: i lead reali non includono `consent`; la UI lo imposta
  automaticamente a ON, con un toggle per testare anche il ramo senza consenso
  (l'agente non parte → gestione operatore).

## API REST

```bash
.venv/Scripts/python -m uvicorn src.main:app --reload
```

- `POST /score` — valuta un `Lead` e restituisce lo `ScoredLead` (idempotente).
- `POST /callback` — ricevitore mock del monolite (ack di consegna).
- `GET /health` — stato, modalità LLM effettiva, storico caricato, versione.

```bash
curl -X POST http://127.0.0.1:8000/score -H "Content-Type: application/json" \
  -d '{"channel":"meta","message":"Vorrei un SUV ibrido, budget 35k, permuto una Golf del 2018. Disponibile per test drive sabato mattina. Pensavo anche a un finanziamento.","vehicle_interest":"Toyota C-HR","city":"Milano","zip_code":"20148","phone":"3471234599","email":"valid@gmail.com","campaign":"SUV Hybrid Q2","created_at":"2026-06-28T10:20:00","consent":true}'
```

## Pesi/soglie e fixture (artifact)

I pesi e le soglie sono **artifact in `config/`** (non hardcoded): lo scorer usa i
**pesi naive** come fallback attivo e userebbe un `config/score_weights.json`
appreso se presente (training **fuori scope**, vedi
[docs/calibration.md](docs/calibration.md)). La fixture dell'estrazione mock si
rigenera con:

```bash
.venv/Scripts/python scripts/build_mock_extractions.py
```

## Attivare OpenAI (opzionale)

Mock-first: l'adapter reale si attiva *solo* con chiave **e** `LLM_MODE=openai`.
Copia `.env.example` in `.env`, imposta `OPENAI_API_KEY` / `LLM_MODE=openai`, e
installa `.[llm]`. Anche con OpenAI attivo l'LLM resta confinato alla **sola
estrazione** (mai score/categoria); timeout/errore → fallback `low_confidence`. In
produzione, per la data-residency EU, target Amazon Bedrock (EU).

## Test

```bash
.venv/Scripts/python -m pytest
```

## Struttura della repo

```
.
├── README.md / REFACTOR_SPEC.md
├── config/                     # artifact: score_weights_naive, category_thresholds,
│                               #           dealer_catchment, blocklists
├── data/                       # leads_mock, leads_history (dedup), mock_extractions (fixture LLM)
├── docs/                       # architettura, progettazione, cost_model, calibration,
│                               # decision_rights, roadmap, diagrammi
├── scripts/                    # run_demo.py, build_mock_extractions.py
├── src/
│   ├── main.py  config.py  logging_setup.py  privacy.py  history.py
│   ├── models/                 # Lead, ExtractedFeatures, ScoreResult, AgentSession, ScoredLead
│   ├── gate/                   # validazione strutturale (no LLM)
│   ├── extraction/             # UNICA call LLM (mock-first) + prompt/schema
│   ├── scoring/                # build_feature_vector + scorer lineare + loader pesi
│   ├── categorization/  motivation/  action/
│   ├── agent/                  # planner, state machine, guardrails, tools, session_store, runner
│   ├── pipeline/               # orchestrazione hot path
│   └── integrations/           # mock: queue, monolith_callback, channels, calendar, inventory, trade_in
└── tests/
```
