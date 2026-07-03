# Lead Scoring Intelligente

Microservizio Python che valuta i lead di un call center automotive (~10.000/giorno)
e assegna a ciascuno **score 0–100**, **categoria** (`hot` / `warm` / `cold` /
`invalid`), **motivazione** e **azione consigliata**. Per i lead di valore avvia un **agente** che li porta a conclusione (recupero info mancanti o
prenotazione test drive). Serve a dare agli operatori una **priorità di chiamata** e a
**ridurre i costi**: gli `invalid` escono dalla coda, i lead migliori sono auto-gestiti.

Due zone:

- **Scorig** — deterministico, **una sola call LLM** di estrazione dal testo della lead: gate → estrazione → scoring lineare → categoria → motivazione → azione.
- **Agente** — asincrono, fuori dal fluisso di scoring: tool mockati e planner LLM.

Flusso, variabili di scoring, calibrazione dei pesi e stima costi in
**[deliverables/](deliverables/)**.

## Requisiti

- Python ≥ 3.11.

## Setup

```bash
py -3 -m venv .venv                                # Linux/macOS: python3 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"    # Linux/macOS: .venv/bin/python
```

Extra opzionali: `.[llm]` (client OpenAI), `.[cli]` (rich), `.[ui]` (streamlit).

## Avvio

```bash
.venv/Scripts/python -m pytest                         # test
.venv/Scripts/python -m uvicorn src.main:app --reload  # API REST: /score, /callback, /health
```

## LLM: mock di default, OpenAI opzionale

Di default l'estrazione gira in **mock** (fixture deterministiche in
`data/mock_extractions.json`, nessuna spesa). Per usare il modello reale serve una
**OpenAI API key**: copia `.env.example` in `.env`, imposta `OPENAI_API_KEY` e
`LLM_MODE=openai`, installa `.[llm]`. Senza chiave — o senza `LLM_MODE=openai` — resta
in mock

## Cambiare provider (es. AWS Bedrock)

Tutte le chiamate al provider sono confinate in un unico punto: `LLMAdapter` in
[src/extraction/llm.py](src/extraction/llm.py). Per passare a un altro provider — es.
**Amazon Bedrock**, utile per la data-residency EU su AWS — basta riscrivere lì le due
chiamate API (`_openai_extract` per l'estrazione e `complete_tool_call` per il planner
dell'agente). Il resto della pipeline non cambia.

## Struttura

```
config/        artifact: pesi, soglie, catchment, blocklist
data/tests     leads di prova per usare l'app streamlit
deliverables/  flusso, calibrazione pesi, costi, diagramma
scripts/       run_demo.py, build_mock_extractions.py, estimate_cost.py
src/           gate, extraction (LLM), scoring, categorization, motivation,
               action, agent, pipeline, integrations (mock)
tests/         suite pytest (offline)
```
