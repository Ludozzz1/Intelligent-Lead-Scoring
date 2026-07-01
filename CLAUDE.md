# CLAUDE.md — Lead Scoring intelligente (AutoXY assignment)

Repo per l'assignment **"Lead scoring intelligente per valutazione lead"**: un sistema che valuta i lead in ingresso e li prioritizza per il call center.

## Regole di lavoro (leggi sempre per prime)

1. **Saluto — REGOLA FONDAMENTALE**: inizia **ogni** risposta rivolgendoti all'utente con il suo titolo: `Zed, master of shadows!`
2. **Fatti, non probabilità**: ogni risposta e proposta è ancorata ai vincoli e ai dati di questo documento (numeri, stack, SLA) o al codice della repo. Se un dato manca, dichiaralo esplicitamente e verificalo — niente stime spacciate per certezze.
3. **Concisione**: risposte il più sintetiche possibile, ma chiare e precise.
4. **Codice**: modifiche minimali e pulite. Niente shortcut che creino debito tecnico.

## Obiettivo

Assegnare a **ogni lead**:
- **score** `0–100`
- **categoria**: `hot | warm | cold | invalid`
- **motivazione** sintetica
- **azione consigliata**: `lead valido | chiedere info mancanti | scartare`
- possibilità di **azioni automatiche / interazioni con l'utente**

Doppio fine di business:
- dare agli operatori una **priorità di chiamata** in base alla bontà del lead;
- **ridurre i costi** del call center gestendo in automatico i lead di alta qualità e scartando gli invalid.

## Fatti & vincoli

- **Volume**: 10.000 lead/giorno ≈ ~7 lead/min di media (attesi picchi sopra la media).
- **Costo call center**: 4 €/chiamata → baseline ~40.000 €/giorno se chiamati tutti. Lo scoring riduce le chiamate (auto-gestione `hot`, scarto `invalid`). *Valori derivati dai fatti, da raffinare con lo storico.*
- **SLA**: esito entro **2 minuti** per lead; oltre, il tasso di qualifica crolla → è il budget di latenza end-to-end.
- **Stack esistente**: dashboard monolite **Vue.js**, backend **Java**, **EKS/Kubernetes** su **AWS**.
- **Dati**: disponibile lo **storico** dei lead.
- **Input lead**: campi strutturati + testo libero (`message`). Schema canonico di riferimento:

```json
{
  "platform": "DriveK",
  "channel": "meta",
  "message": "Vorrei un SUV ibrido, budget 35k, permuto una Golf del 2018. Disponibile per test drive sabato.",
  "vehicle_interest": "Toyota C-HR",
  "city": "Milano",
  "zip_code": "20148",
  "phone": "3470134573",
  "name": "Mario",
  "surname": "Rossi",
  "email": "mario.rossi@example.it",
  "campaign": "SUV Hybrid Q2",
  "created_at": "2026-06-20T10:20:00"
}
```

## KPI di qualità (criteri di valutazione)

Tenere sempre presenti: **costi**, **latenza**, **privacy**, qualità/accuratezza dello scoring, gestione degli errori.

## Deliverable della consegna

- Codice core **funzionante e semplificato**, con **dati mockati** per le integrazioni esterne.
- Diagrammi/grafici, formule, previsioni di costo.
- `README.md` con istruzioni di startup.
- (Live) Presentazione tecnica: architettura, scelte tecnologiche, gestione errori, KPI.

## Struttura repo & convenzioni

Architettura a **due zone** allineata a `REFACTOR_SPEC.md` (lo scope autoritativo).

- **Hot path** (deterministico, ≤2 min, **una sola call LLM**): `src/gate/` →
  `src/extraction/` (unica call LLM, mock-first via fixture) → `src/scoring/`
  (`build_feature_vector` condivisa + scorer lineare a **pesi naive**) →
  `src/categorization/` → `src/motivation/` (deterministica) → `src/action/`.
  Orchestrazione in `src/pipeline/`.
- **Zona agentica** (async, event-driven, fuori SLA): `src/agent/` (state machine,
  policy, guardrails, session_store, runner, tool mockati).
- **Cross-cutting**: `src/privacy.py` (redazione PII), `src/history.py` (solo
  dedup/personalizzazione), `src/integrations/` (mock boundary).
- **Artifact** in `config/` (`score_weights_naive.json`, `category_thresholds.json`,
  `dealer_catchment.json`, `blocklists.json`). I pesi sono **naive** (training fuori
  scope, vedi `docs/calibration.md`).

Convenzioni: **codice in inglese, documentazione in italiano**. Test deterministici
(mock-first, nessuna API key).

Comandi (venv): `./.venv/Scripts/python.exe -m pytest` (test) ·
`./.venv/Scripts/python.exe scripts/run_demo.py` (demo) ·
`./.venv/Scripts/python.exe cli.py` (review CLI) ·
`./.venv/Scripts/python.exe -m uvicorn src.main:app --reload` (API) ·
`./.venv/Scripts/python.exe scripts/build_mock_extractions.py` (rigenera fixture LLM).

Anti-pattern da evitare (REFACTOR_SPEC §11): >1 call LLM nel hot path, LLM-as-judge,
agente dentro lo SLA, score black-box, soglie a intuito, `invalid` come banda,
storico a runtime per lo scoring, PII grezzi all'LLM, nomi auto hardcoded.
