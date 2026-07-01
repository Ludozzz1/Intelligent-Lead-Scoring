# Progettazione - Lead Scoring Intelligente

Documento di progettazione in due parti:

- **Parte 1 - Architettura**: panoramica a due zone, mappa dei componenti, flusso
  end-to-end, cross-cutting concerns (privacy, errori, latenza), stack.
- **Parte 2 - Scelte progettuali e trade-off**: "perche X e non Y" per ogni
  decisione, da difendere in presentazione.

Per il dettaglio modulo-per-modulo e la gestione errori vedi
[architecture.md](architecture.md); per i diagrammi
[diagrams/flow_e2e.txt](diagrams/flow_e2e.txt) e
[diagrams/scoring_axes.txt](diagrams/scoring_axes.txt).

---

# Parte 1 - Architettura

## 1.1 Panoramica

Microservizio Python (FastAPI) presentato come **add-on** del monolite esistente
(Vue.js + backend Java su EKS/Kubernetes su AWS): consuma un lead, lo valuta e
riscrive il risultato al monolite via callback REST. La dashboard Vue legge solo i
nuovi campi (score, priorita, motivazione) sul record gia mostrato — **nessuna
riscrittura del frontend, nessun Java toccato**.

L'idea centrale e separare il sistema in **due zone** con vincoli opposti:

1. **Zona 1 - Hot path** (REFACTOR_SPEC §5): **deterministica**, dentro lo SLA di 2
   minuti, con **esattamente una call LLM** (la structured extraction). Tutto il
   resto — gate, scoring, categoria, motivazione, azione — e regole. Lo score e
   corretto per costruzione e auditabile; l'LLM e sempre degradabile.
2. **Zona 2 - Agente** (REFACTOR_SPEC §7): **asincrona, event-driven, decoupled**,
   FUORI dallo SLA. Un singolo Lead-Resolution Agent porta i lead promettenti a uno
   stato terminale (prenotazione / info completate / handoff / non-risposta).

Una terza dimensione e **OFFLINE e solo documentata**: la calibrazione dei pesi
appresi ([calibration.md](calibration.md)). Il training e **fuori scope**; i **pesi
naive** sono il fallback attivo.

## 1.2 Mappa dei componenti

| Livello | File | Responsabilita |
|---|---|---|
| Entrypoint API | [src/main.py](../src/main.py) | FastAPI: `POST /score`, `POST /callback`, `GET /health`. Thin: delega alla pipeline. |
| Orchestrazione Zona 1 | [src/pipeline/pipeline.py](../src/pipeline/pipeline.py) | `Pipeline.score_lead`: stage guardati (degrada, non lancia), idempotenza. NON esegue l'agente. |
| Config | [src/config.py](../src/config.py) | `Settings` (pydantic-settings): solo knob di runtime. Pesi/soglie sono artifact. |
| Modelli | [src/models/](../src/models/) | `Lead`, `ExtractedFeatures`, `FeatureVector`, `ScoreResult`, `ValidityResult`, `Personalization`, `AgentSession`, `ScoredLead`. |
| Gate | [src/gate/validity.py](../src/gate/validity.py) | Validazione strutturale binaria, NO LLM, blocklist esternalizzata. |
| Estrazione | [src/extraction/](../src/extraction/) | La sola call LLM, gated + PII-safe; mock-first via fixture; fallback deterministico. |
| Feature vector | [src/scoring/feature_vector.py](../src/scoring/feature_vector.py) | `build_feature_vector` condivisa (anti-skew), 9 feature §5.3. |
| Scorer | [src/scoring/scorer.py](../src/scoring/scorer.py) | Combinazione lineare + contributi per-feature. |
| Pesi/soglie | [src/scoring/weights.py](../src/scoring/weights.py) | Loader artifact (`learned` preferito, `naive` fallback). |
| Categoria | [src/categorization/bands.py](../src/categorization/bands.py) | Bande hot/warm/cold; invalid dal gate o looks_invalid. |
| Motivazione | [src/motivation/motivation.py](../src/motivation/motivation.py) | Deterministica (no 2a call LLM). |
| Azione | [src/action/decision.py](../src/action/decision.py) | Azione consigliata + trigger agente + priority. |
| Agente (Zona 2) | [src/agent/](../src/agent/) | State machine, guardrails, tool mockati, session store, runner. |
| Storico | [src/history.py](../src/history.py) | Solo dedup + personalizzazione a runtime (no scoring). |
| Privacy | [src/privacy.py](../src/privacy.py) | Redazione PII, whitelist campi, chiavi dedup (sha256). |
| Integrazioni | [src/integrations/](../src/integrations/) | Boundary mockati: queue+DLQ, callback, channels, calendar, inventory, trade-in. |
| Dati | [data/](../data/) | 26 lead mock; 500 record storici (dedup); fixture estrazione mock. |
| Config artifact | [config/](../config/) | pesi naive, soglie, catchment, blocklist. |
| Demo | [scripts/run_demo.py](../scripts/run_demo.py) | End-to-end a due zone (hot path + agente con repliche simulate). |

## 1.3 Flusso end-to-end

Diagramma completo in [diagrams/flow_e2e.txt](diagrams/flow_e2e.txt). In sintesi,
`Pipeline.score_lead` esegue (ogni stage guardato):

```
(1) lead_id + idempotenza  hash phone_key|email_key|created_at; cache hit -> ScoredLead (duplicate)
(2) gate                   regole, NO LLM: valido | invalid (STOP senza LLM)
(3) personalizzazione      rilettura storico: duplicato / cliente di ritorno
(4) estrazione             LA SOLA call LLM (gated + PII-redacted) o fallback low_confidence
(5) feature vector+score   build_feature_vector (condivisa) -> prodotto scalare coi pesi naive
(6) categoria              bande hot/warm/cold; invalid da gate o looks_invalid
(7) motivazione            deterministica (rationale_signals + top contributi)
(8) azione                 lead_valido|chiedere_info|scartare + (eventuale) trigger agente + priority
```

Lo score torna **subito** (con al piu un trigger) e va al monolite via
`MockMonolithCallback`. **La Zona 2 e separata**: solo i lead hot/warm con
interazione aperta diventano una `AgentSession` che il runner avanza sugli eventi —
fuori dallo SLA. Le due vie d'ingresso sono `POST /score` (sincrona) e la **coda
mock** (`run_demo.py`: `InMemoryQueue` -> `consume_all` -> pipeline -> callback).

## 1.4 Cross-cutting concerns

**Privacy (by design).** Le PII non lasciano mai il processo verso LLM o log:
- il `message` e **redatto** (`redact_message`) *prima* di toccare l'adapter, con
  guardia `assert_no_raw_pii` che solleva in caso di leak;
- al modello va solo una **whitelist** esplicita di campi non-PII
  (`safe_fields_for_llm`): mai phone/email/name/surname; lo ZIP e ridotto a 3 cifre;
- i destinatari outbound dell'agente sono **token opachi** (sha256), mai grezzi;
- **residency EU**: la sola call LLM e mappabile su Bedrock EU (GDPR);
- lo score e **deterministico e auditabile** (contributi per-feature + motivazione)
  -> pronto al GDPR Art. 22 (decisione automatizzata: logica ispezionabile).

**Gestione errori.** Dettaglio in [architecture.md](architecture.md). In sintesi:
ogni stage della pipeline degrada a un default sicuro; l'estrazione ripiega a un
default solo-strutturale `low_confidence`; l'adapter LLM ha timeout (8 s) e circuit
breaker; la coda isola i poison message in **DLQ**; idempotenza/dedup sul `lead_id`;
nell'agente ogni errore di tool o budget superato -> handoff umano, mai loop.

**Latenza / SLA.** Budget end-to-end **2 minuti** per lead. L'hot path e
microsecondi di aritmetica + **una** call LLM gated; nel peggiore dei casi (LLM al
limite del timeout) si resta intorno a 8-10 s, **un ordine di grandezza sotto i 2
minuti**. L'LLM **non e mai nell'aritmetica dello score**: un timeout degrada a
`low_confidence`, non rompe ne ritarda lo SLA. Analisi p50/p95/p99 in
[cost_model.md](cost_model.md). L'**agente e fuori dallo SLA** per definizione.

**Osservabilita.** Logging strutturato, una riga per lead (lead_id, categoria,
score, priorita, azione, provenienza estrazione, latenza). Ogni azione dell'agente
e un `AgentAction` con stato e motivazione (audit trail in `AgentSession.actions`).
`GET /health` espone modalita LLM effettiva e stato storico.

## 1.5 Stack tecnologico

| Area | Take-home (implementato) | Produzione (target documentato) |
|---|---|---|
| Linguaggio | Python (3.11+, testato 3.13) | idem |
| API | FastAPI + uvicorn | idem, su EKS/Kubernetes |
| Validazione | pydantic v2 + pydantic-settings | idem |
| Ingestion | `InMemoryQueue`/`FileQueue` + DLQ | Amazon SQS + DLQ |
| LLM | adapter mock-first (fixture); OpenAI dietro flag | Amazon Bedrock (EU) / Azure OpenAI EU, Haiku-class |
| Scoring | lineare su `build_feature_vector` + pesi naive | + pesi appresi (logistic) da calibrazione offline |
| Storage | JSON su file; session store in-memory/file | Postgres (RDS/Aurora) + S3/Redshift (training) |
| Callback | mock REST verso il monolite | REST verso backend Java + push WebSocket alla dashboard Vue |
| Canali/calendar/inventory/permuta | mock deterministici | provider reali (Twilio/SES), scheduling dealer, DMS, servizio permuta |
| Segreti | `.env` | secret manager |
| Osservabilita | logging strutturato + redazione PII | + audit log immutabile (GDPR Art. 22) |

---

# Parte 2 - Scelte progettuali e trade-off

Per ogni decisione: la scelta, l'alternativa scartata e la motivazione.

## 2.1 Hot path deterministico + UNA sola call LLM

**Scelta**: hot path interamente a regole con **esattamente una** call LLM (la
structured extraction). Score, categoria, motivazione e azione sono deterministici.
**Alternativa**: piu call LLM (un secondo LLM che valuta/spiega lo score, o l'LLM
che assegna direttamente lo score).
**Perche**: uno score calcolato da regole e **corretto per costruzione,
riproducibile e auditabile** (GDPR Art. 22) — non si "verifica" con un secondo LLM
(anti-pattern §11). Confinando l'LLM alla sola estrazione: (a) il costo e
controllato (un'unica call piccola per lead valido); (b) lo score e **sempre
calcolabile** anche se l'LLM e lento o giu (fallback `low_confidence`); (c) lo SLA
non dipende mai da un provider esterno. E l'**invariante duro** del sistema.

## 2.2 Estrazione semantica delegata all'LLM (vs keyword/regex hardcoded)

**Scelta**: la comprensione del testo libero (modello desiderato, urgenza,
sentiment, "sembra invalido?") e **compito dell'LLM**; il mock e una **fixture map**
message->`ExtractedFeatures` ([data/mock_extractions.json](../data/mock_extractions.json)).
**Alternativa**: un parser keyword/regex "fake NLP" come quello legacy.
**Perche**: le regole keyword sono fragili e non scalano sulla varieta del
linguaggio naturale — era un anti-pattern del primo design. La semantica e
**esattamente** cio per cui l'LLM ha valore; tutto il resto resta deterministico.
La fixture e un mock onesto (nessuna euristica nascosta): un messaggio non noto
ritorna `low_confidence`, esercitando il path di degradazione. **Importante**: NON
esiste un fallback keyword — su errore LLM si scora sui soli campi strutturali, non
si finge una NLP.

## 2.3 Gate strutturale deterministico (vs giudizio nel gate)

**Scelta**: il gate ([validity.py](../src/gate/validity.py)) fa **solo** check
strutturali binari (telefono/email plausibili, canale raggiungibile, blocklist
esternalizzata), **prima** dell'LLM. Il giudizio semantico (spam/gibberish/fuori
area) e `looks_invalid` dell'LLM, **dopo** il gate.
**Alternativa**: mettere il giudizio "e spam?" tra le regole del gate, o trattare
`invalid` come una banda di score.
**Perche**: separare *struttura* (deterministica, a costo zero, fail-fast — risparmia
la call LLM sui lead spazzatura) da *semantica* (serve l'LLM) tiene ogni decisione
nel posto giusto. `invalid` e un **gate**, non una banda (anti-pattern §11): un lead
falso non ha "qualita" da misurare. Il **consenso non e nel gate**: gestisce
l'invio outbound dell'agente, non la scorabilita.

## 2.4 `build_feature_vector` condivisa (anti training/serving skew)

**Scelta**: **una sola** funzione deterministica costruisce il vettore di feature,
pensata per essere riusata verbatim dal training offline.
**Alternativa**: feature-building duplicato (uno per il runtime, uno per il
training) o scoring sul testo grezzo.
**Perche**: se training e runtime costruiscono le feature in modo anche
leggermente diverso, il modello apprende su una distribuzione che non rivede in
produzione (**training/serving skew**) — bug classico e domanda da colloquio. Avere
**una** funzione, sullo **stesso** vettore nominato, elimina il problema per
costruzione. E il motivo per cui lo scorer e lineare su un vettore esplicito e non
una scatola di if.

## 2.5 Pesi naive come fallback attivo (training fuori scope)

**Scelta**: lo scorer usa **pesi naive** tarati a mano
([config/score_weights_naive.json](../config/score_weights_naive.json)); il loader
userebbe un artifact appreso `config/score_weights.json` se presente, ma **il
training e fuori scope** ([calibration.md](calibration.md)).
**Alternativa**: addestrare ora un modello sui dati (sintetici), o uno score
black-box.
**Perche**: su dati mock l'AUC non e significativa; addestrare sarebbe teatro. Il
valore architetturale e la **metodologia pronta** (label -> `build_feature_vector`
-> logistic -> backtest) e lo **scorer gia calibrabile** deponendo un file, senza
toccare codice. I pesi devono restare **interpretabili** (coefficienti lineari, no
gradient boosting/DNN): l'explainability e un KPI.

## 2.6 Agente decoupled (vs sincrono dentro lo score)

**Scelta**: l'agente e **fuori** da `score_lead`, **event-driven**, su sessioni
**persistite** ([session_store.py](../src/agent/session_store.py)); `score_lead`
imposta solo un trigger e ritorna.
**Alternativa**: eseguire l'agente sincrono dentro la pipeline di scoring (come nel
legacy).
**Perche**: l'interazione con l'utente vive su orizzonte lungo (risposte dopo
minuti/ore/mai) — incompatibile con lo SLA di 2 minuti e con l'alto volume. Un loop
non deterministico **dentro** il percorso a SLA e un anti-pattern (§11). Decoupling:
lo score resta veloce e deterministico; l'agente si "sveglia" sugli eventi e puo
prendersi tutto il tempo necessario. Booking = **azione terminale** (human-approval
in v1), non un secondo agente; recupero info e negoziazione = **la stessa** state
machine su traiettorie diverse.

## 2.6bis Routing allineato al valore + loop di arricchimento (vs trigger "a forma")

**Scelta**: il trigger dell'agente è guidato dal **valore del lead + consenso**
([decide_action](../src/action/decision.py) / `route_complete`), non dalla semplice
presenza di un'interazione aperta. Un lead **incompleto** (qualsiasi banda) viene
recuperato; un **completo** automation-worthy (`hot`, o `warm ≥ warm_high`) tenta il
**booking proattivo**; un **cold completo** riceve **nurturing automatico**; senza
consenso il lead va all'operatore (consenso valutato **a monte**). Dopo un recupero info
l'agente **ri-score** la risposta riusando la **stessa** `build_feature_vector` e
ri-instrada con la **stessa** `route_complete` (sorgente unica, hot path + agente).
**Alternativa**: il trigger v0 ("solo `hot`/`warm` con `availability_mentioned` o info
mancanti; `invalid`/`cold` mai"); nessun re-scoring (l'agente chiudeva in `COMPLETED_INFO`
senza ricalcolare la categoria).
**Perché**: l'obiettivo di business è *"gestire in automatico i lead di alta qualità"* —
agganciare il trigger alla *forma* (c'è un orario citato?) e non al *valore* lasciava gli
`hot` completi senza disponibilità in coda operatore (€4 sul lead migliore) e i `cold` con
una disposizione solo dichiarata nei doc, mai implementata. Riusare **una** funzione di
routing e **una** `build_feature_vector` per hot path e re-scoring elimina la duplicazione
(stesso anti-skew di §2.4). Il re-scoring vive **fuori SLA** e **non aggiunge call LLM**
(sfrutta il `re_extract` già previsto). L'agente **non disqualifica** mai: un cold ancora
debole va in `nurturing`, non in `invalid` (anti-pattern §11). È una **deviazione
deliberata** da REFACTOR_SPEC §7.1 (aggiornato di conseguenza).

## 2.7 Un solo agente con state machine esplicita (vs multi-agente)

**Scelta**: **un** Lead-Resolution Agent come state machine
([state_machine.py](../src/agent/state_machine.py)) con guardrail e diritti di
decisione dichiarativi.
**Alternativa**: orchestrazione multi-agente.
**Perche**: il multi-agente aggiunge orchestrazione, costo e non-determinismo senza
valore a questo scope. Una state machine esplicita e gia "agentica" (selezione
autonoma del tool per stato, negoziazione, controproposte) ed e **testabile,
auditabile e con stop conditions chiare** (budget passi/messaggi, handoff). Il
multi-agente e citato come evoluzione futura ([roadmap.md](roadmap.md)).

## 2.8 Storico solo a runtime per dedup/personalizzazione (no scoring)

**Scelta**: a runtime lo storico ([history.py](../src/history.py)) fa **solo** dedup
e personalizzazione (cliente di ritorno); **non** alimenta lo score.
**Alternativa**: usare il prior storico di conversione/invalid come feature di
scoring online (come faceva il `source_quality`/risk legacy), o "lead simili" via
retrieval.
**Perche**: lo storico nel runtime e un anti-pattern (§11): accoppia lo score a una
query stateful, complica latenza e riproducibilita, e rischia leakage. Gli esiti
storici sono preziosi ma vanno usati **offline** (calibrazione dei pesi), non a
runtime. A runtime lo storico serve solo a cose **non-score**: evitare doppioni e
riconoscere un cliente che converte (boost di priorita).

## 2.9 Inventario come tool dell'agente (vs catalogo nello scoring)

**Scelta**: la disponibilita veicolo e un **tool** dell'agente
([inventory.py](../src/integrations/inventory.py)); lo scoring non conosce nessun
catalogo.
**Alternativa**: il sub-score "Fit" legacy con catalogo auto e stock hardcoded nel
codice di scoring.
**Perche**: un catalogo hardcoded e debito tecnico (cambia di continuo, non
appartiene allo score). La disponibilita conta quando si **negozia un appuntamento**
— cioe nella zona agentica, dove e un tool mockabile dietro interfaccia. Lo scoring
resta pulito sulle 9 feature §5.3.

## 2.10 Mock-first vs integrazione reale obbligatoria

**Scelta**: il sistema gira **senza alcuna chiave**, con mock deterministici dietro
`Protocol`; OpenAI dietro un adapter unico, attivo solo con key + `llm_mode=openai`.
**Alternativa**: richiedere chiavi/servizi reali per girare.
**Perche**: la demo deve essere **riproducibile con un comando** e i test non devono
dipendere da rete/costi. Tutti i confini esterni (coda, callback, canali, calendar,
inventory, permuta) sono sostituibili: in produzione si scambia l'implementazione
senza toccare la logica (vedi la tabella di mappatura AWS in
[architecture.md](architecture.md)).
