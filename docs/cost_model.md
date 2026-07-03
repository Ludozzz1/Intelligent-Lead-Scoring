# Modello di costo, ROI e analisi SLA

Tutti i numeri sono **derivati dai fatti** dell'assignment (10.000 lead/giorno,
4 EUR/chiamata, SLA 2 minuti, REFACTOR_SPEC §1) o **misurati/derivati** sul dataset
demo; le assunzioni di business sono dichiarate esplicitamente e vanno raffinate con
lo storico reale. Le formule sono parametriche: cambiando le quote, i numeri si
ricalcolano.

## 1. Baseline: il costo da battere

| Voce | Formula | Valore |
|---|---|---|
| Volume | dato | 10.000 lead/giorno |
| Costo per chiamata | dato | 4 EUR |
| **Baseline (chiamare tutti)** | 10.000 x 4 EUR | **40.000 EUR/giorno** |
| Baseline mensile | 40.000 x 30 | **~1,2 M EUR/mese** |
| Ritmo medio | 10.000 / 86.400 s | ~0,12 lead/s (~7 lead/min) |

Oggi gli operatori chiamano **alla cieca**: ogni lead, buono o spazzatura, costa
4 EUR. E la slide-titolo: lo scoring esiste per evitare le chiamate inutili.

## 2. Costo del sistema (parametrico)

Due voci: **token LLM** e **compute worker**.

### 2.1 Token LLM — UNA sola call per lead valido

L'architettura prevede **esattamente una** call LLM nel hot path (la structured
extraction) e **nessun secondo LLM** (la motivazione e deterministica — l'explainer
legacy e stato rimosso). La call e inoltre **gated**: lead invalid o message banali
non la fanno ([extraction.extractor](../src/extraction/extractor.py)).

```
token_per_lead_gated ~= ~250 in + ~60 out  ~= 310 token / lead che passa il gate
```

Assunzione: ~60-70% dei lead passa il gate (gli `invalid` e i message banali non
chiamano l'LLM). Su 10.000 lead/giorno -> ~6.000-7.000 call/giorno.

```
token/giorno ~= 7.000 x 310  ~= 2,2 M token/giorno  ~= 65 M token/mese
```

Con un modello **Haiku-class** (veloce/economico, ~0,25-1,25 USD per 1M token
input/output a seconda del provider e del mix), il costo LLM mensile e nell'ordine
di **~50-150 EUR/mese**. Dimezzare le call (una sola, non due) **dimezza** questa
voce rispetto al design legacy. (Da raffinare col prezzo effettivo del provider EU.)

> Assunzione esplicita: endpoint LLM in **regione EU** (mappabile su Amazon Bedrock
> EU) per la data-residency GDPR — vincolo di privacy, non di costo, ma orienta la
> scelta del provider.

### 2.2 Compute worker

A ~0,12 lead/s medi (picchi ~1-2/s) e una latenza per-lead di **microsecondi** di
aritmetica (vedi §4), **un singolo worker** regge il volume con enorme margine. Per
ridondanza/HA si dimensionano 2-3 pod piccoli su EKS: ordine di
**~300-600 EUR/mese** (compute + rete + storage Postgres gestito).

### 2.3 Totale sistema

| Voce | Stima mensile |
|---|---|
| Token LLM (Haiku-class, 1 call gated) | ~50-150 EUR |
| Compute/infra (2-3 pod piccoli) | ~300-600 EUR |
| **Totale sistema** | **~350-750 EUR/mese** |

Da confrontare con **~1,2 M EUR/mese** di baseline: il sistema costa **~0,03-0,06%**
del costo che governa. Il costo del sistema e trascurabile; il valore e nel risparmio.

### 2.4 Costo dell'agente (zona 2 — planner LLM opzionale)

L'agente puo girare con un **planner LLM** (orchestrazione conversazionale, non
scoring). Il costo aggiuntivo e contenuto e **disaccoppiato dallo SLA**:

- **Solo sui lead *triggered***: hot/warm con interazione aperta — una **frazione**
  del volume (nel dataset demo ~17/26). Gli `invalid`/`cold` non attivano l'agente.
- **Default mock**: in `llm_mode=mock` il planner e **deterministico** → **zero
  token** (il caso del take-home e di gran parte del traffico a basso rischio).
- **Tetto per sessione**: `agent_max_llm_calls` (8) × `agent_max_turns` limitano le
  call; stima `costo_sessione ≈ call_LLM × token_per_call`. Con un modello
  Haiku-class (~310 token/call), una sessione tipica (~3-6 call) e nell'ordine di
  **frazioni di centesimo**; anche assumendo l'agente LLM su **tutti** i ~6-7k lead
  triggered/giorno, l'ordine di grandezza resta **decine di EUR/mese** — sotto il
  risparmio di **una manciata** di chiamate evitate.
- **Fuori SLA**: piu call qui **non** toccano il budget di latenza dei 2 minuti
  (l'agente e async); su errore/timeout il loop **degrada** al planner deterministico.

> In sintesi: l'agente LLM e un costo **marginale e governato** (gated + tetto +
> degrade), giustificato dall'uplift di conversione (booking auto-gestiti) — da
> raffinare con il tasso di trigger e il prezzo reale del provider EU.

## 3. Risparmio: scartare gli invalid e auto-gestire i lead di valore

Il risparmio si materializza nella **coda operatore** (`queue`,
[suggestions.py](../src/action/suggestions.py)): una chiamata è evitata solo quando il
lead **non entra nella coda attiva**. Due fonti, allineate al valore + consenso
([decide_action](../src/action/decision.py)):

- **invalid → `queue="scartato"`**: **fuori dalla coda di chiamata** (0 chiamate). Non
  più un consiglio "scartare" su un lead che resta in lista: è una **disposizione di
  sistema**, con motivazione conservata solo per audit/appello.
- **auto-gestiti dall'agente → `queue="agente"`**: solo lead ad alto valore — consenso
  e `score ≥ warm_high` (gli `hot` sono sempre sopra), chiusi con `BOOKED`
  (booking staged + approvazione umana). Gli **incompleti** passano prima dal
  `recover_info`: l'arricchimento (re-scoring §7.2) può promuoverli a booking-worthy.
  Warm medio/basso e `cold` **non entrano mai in automazione**.
- **tutto il resto → `queue="attiva"`**: senza consenso (l'agente non può messaggiare),
  warm medi/bassi, e i lead che l'agente **ri-emerge** dopo handoff / approvazione /
  nessuna risposta. Restano in coda **ma priorizzati**, con un `next_best_action`.

Sul **dataset demo (26 lead, consent-rich)** le chiamate evitate sono **5 scartati + 15
auto-gestiti = 20/26 (~77%)**. È un limite superiore: a regime l'evitato dipende dalla
**quota invalid**, dal **tasso di consenso** e dalla **quota hot/warm-alti** (i `cold` e i
warm medi restano coda operatore).

Assumendo, in modo prudenziale, **~30-40%** di chiamate evitabili a regime (invalid +
auto-gestiti con consenso):

```
chiamate_evitate/giorno = 10.000 x 35%  ~= 3.500
risparmio/giorno        = 3.500 x 4 EUR ~= 14.000 EUR/giorno
risparmio/mese          = 14.000 x 30   ~= 420.000 EUR/mese
```

Con il range 30-40%:

```
30% -> 3.000 chiamate -> 12.000 EUR/g -> ~360.000 EUR/mese
40% -> 4.000 chiamate -> 16.000 EUR/g -> ~480.000 EUR/mese
```

**Risparmio stimato: ~360.000-480.000 EUR/mese** (i risparmi vengono da invalid scartati
e da hot/warm-alti auto-gestiti; i `cold` ora restano coda operatore, priorizzati in
basso). A questo si aggiunge un effetto qualitativo: la priorizzazione concentra il tempo
operatore sui lead buoni (non quantificato qui, prudenziale).

## 4. ROI e break-even

| Voce | Valore mensile |
|---|---|
| Baseline (chiamare tutti) | ~1.200.000 EUR |
| Costo del sistema | ~350-750 EUR |
| Risparmio lordo (invalid scartati + auto-gestiti) | ~360.000-480.000 EUR |
| **Risparmio netto** | **~359.000-479.000 EUR/mese** |
| **ROI** | risparmio/costo ~= **480x-1.300x** |

**Break-even**: il sistema costa ~350-750 EUR/mese ed evita gia ~12.000-16.000 EUR
di chiamate *al giorno*. Si ripaga in **meno di 2 ore** di operativita al primo
giorno. Il ROI e schiacciante: e la slide d'apertura.

> Assunzioni da raffinare con lo storico: quota effettiva invalid, tasso di consenso,
> quota hot/warm-alti auto-gestibili, uplift di conversione dalla priorizzazione.

## 5. Analisi SLA e latenza

**Budget end-to-end: 2 minuti per lead.** Oltre, il tasso di qualifica crolla (un
compratore caldo non aspetta). E il budget di latenza, non il throughput. Vale per
lo **score**; l'**agente e fuori dallo SLA** per definizione (event-driven, async).

### 5.1 Budget per stage (hot path)

| Stage | Componente | Costo tipico |
|---|---|---|
| Gate (validita) | regole pure | microsecondi |
| Estrazione | **l'unica call LLM** (gated) o fallback | ~1-3 s (LLM) / microsecondi (mock/fallback) |
| build_feature_vector + scorer | prodotto scalare | microsecondi |
| Categoria + motivazione + azione | regole + template | microsecondi |
| Callback | mock REST | microsecondi (mock) / ~50-200 ms (prod) |

La **sola call LLM domina** la latenza reale, ma e nel percorso critico **in
sicurezza** perche: (a) e gated (gli invalid/message banali non la fanno), (b) ha
timeout duro (`llm_timeout_s=8.0`), (c) **non e nell'aritmetica dello score** — su
lentezza/timeout lo score esce comunque dai soli campi strutturali, flaggato
`low_confidence`. Caso peggiore ~8-10 s, **un ordine di grandezza sotto i 2 minuti**.

### 5.2 Stima p50/p95/p99 (produzione con LLM EU)

| Percentile | Stima e2e | Note |
|---|---|---|
| p50 | ~1-2 s | un lead valido medio fa una call LLM rapida |
| p95 | ~3-5 s | message lunghi / coda provider |
| p99 | ~8-10 s | timeout LLM -> fallback deterministico; lo score esce comunque |
| % entro SLA (2 min) | **~100%** | il p99 e ~12x sotto il budget |
| Tasso fallback/timeout atteso | < 1-2% | conteggiato come KPI di latenza |

In modalita mock (offline, nessuna rete) l'hot path e di **microsecondi** di
aritmetica per lead.

## 6. KPI - 5 famiglie

### 6.1 Business
- Riduzione costo call center (EUR/giorno e %), chiamate evitate/giorno.
- CPQL (costo per lead qualificato), conversione per categoria.
- Funnel lead -> appuntamento (`BOOKED`) -> vendita.

### 6.2 Qualita del modello
- Precision/recall della classe `hot`.
- **False-invalid rate** (il rischio piu costoso: buttare un compratore vero) —
  presidiato dall'invalidazione conservativa del gate.
- Concordanza decisione automatica <-> operatore.
- (Offline, fuori scope) AUC e calibrazione score<->conversione dopo il training dei
  pesi appresi ([calibration.md](calibration.md)).

### 6.3 Latenza / SLA
- p50/p95/p99 end-to-end, % entro SLA (target ~100%).
- Tasso di timeout/fallback LLM, tasso di dead-letter (DLQ).

### 6.4 Costo infra
- Costo per lead (compute + token), costo mensile token LLM, ROI mensile.

### 6.5 Privacy
- % di call LLM con PII redatta (target 100%, garantito da `assert_no_raw_pii`).
- Data residency EU, retention, tracking del consenso.
- Audit log delle decisioni automatiche (GDPR Art. 22).
