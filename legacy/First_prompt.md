# Crea da zero la repository "Lead Scoring Intelligente"

Genera **da zero** una nuova repository Python completa, funzionante e documentata. Leggi tutto il prompt prima di iniziare; poi ragiona brevemente sull'architettura e procedi. **Non sovra-ingegnerizzare.**

**Lingua:** tutta la **documentazione** (README, `docs/`, commenti esplicativi nei doc) in **italiano**; tutto il **codice** (nomi di variabili/funzioni/classi/moduli, docstring, log, identificatori) in **inglese**.

## 1. Contesto e obiettivo

Sistema di **lead scoring intelligente** per un call center automotive. Ogni giorno arrivano ~10.000 lead, organici (siti web) o da campagne a pagamento (es. Meta). Oggi l'applicativo del call center mostra i lead in arrivo *senza alcuna informazione sulla qualità*: gli operatori chiamano alla cieca, e ogni chiamata costa **4 euro**. Vogliamo: (a) dare agli operatori una **priorità di chiamata** in base alla bontà del lead; (b) **ridurre i costi** intervenendo in automatico sui lead di alta qualità e deprioritizzando/scartando quelli invalidi o freddi.

Esempio di lead in input:
```json
{ "platform":"DriveK", "channel":"meta", "message":"Vorrei un SUV ibrido, budget 35k, permuto una Golf del 2018. Disponibile per test drive sabato.", "vehicle_interest":"Toyota C-HR", "city":"Milano", "zip_code":"20148", "phone":"3470134573", "name":"Mario", "surname":"Rossi", "email":"mario.rossi@example.it", "campaign":"SUV Hybrid Q2", "created_at":"2026-06-20T10:20:00" }
```

Per ogni lead il sistema deve produrre: **score 0-100**, **categoria** (`hot`/`warm`/`cold`/`invalid`), **motivazione sintetica**, **azioni consigliate** (lead valido / chiedere info mancanti / scartare), **rischio + confidenza** espliciti, e le **azioni effettivamente intraprese** dall'agente.

**Insight di scenario che deve guidare le scelte (e che vanno dette a voce nella presentazione):**
- **Volume basso:** 10.000 lead/giorno ≈ **0,12 lead/s** medi, picchi realistici ~1-2/s. Il vincolo NON è scalare → è la **resilienza e il fallback sul singolo lead**. La capacità è un problema banale qui: dichiaralo e non sovradimensionare.
- **SLA generosissimo:** l'esito serve entro **2 minuti**, ma la latenza reale per lead è ~2-5s. Quindi **una chiamata LLM nel percorso critico è ammessa** (ma va gated: vedi §3).
- **Costo da battere:** 10.000 × 4€ = **40.000 €/giorno ≈ 1,2 M€/mese**. Il sistema di scoring costa ~**500-1.000 €/mese**. Evitare il 30-40% di chiamate (invalid + cold) vale **~360-480 k€/mese**. ROI schiacciante: è la slide-titolo (dichiara le assunzioni — quota invalid+cold e fatto che i cold ricevono nurturing, non costo zero).

## 2. OBIETTIVI PRIMARI (criterio di scarto — non negoziabili)

Questa è la seconda versione di una prova tecnica: la prima è stata scartata perché mancavano esattamente questi tre punti. Devono essere **plateali ed evidenti** nel codice, nei doc e nella demo. **Una soluzione che non rende evidenti tutti e tre è da considerare fallita.**

1. **PARTE AGENTICA reale.** Un vero agente con *tool-calling* e azioni autonome (loop decisione → tool → osservazione), **non** una pipeline single-shot. L'agente ragiona su lead + score + storico e decide quali tool invocare, con un gate **human-in-the-loop** per le azioni costose/rischiose.
2. **CALCOLO DEL RISCHIO quantificato.** Output di rischio **numerici e calibrati sullo storico**: `P(invalid/fraud)` e `P(conversione)` con **confidenza**. Non un gate binario keyword/LLM. Anche se nell'MVP il modello è semplice (frequenze condizionate o regressione logistica leggera), il rischio deve essere un **output esplicito e calibrato**, non un dettaglio implicito.
3. **UTILIZZO DEI CUSTOMER_DATA / STORICO.** Il sistema **rilegge lo storico** per **due usi distinti**: (a) **personalizzazione** (lead già visto, duplicato, cliente ricorrente, comportamento passato) che influenza score, azioni, priorità e il *testo* dei messaggi dell'agente; (b) **calibrazione del rischio** (prior di conversione per canale/campagna/platform, tasso di invalid). Non basta iniettare pochi campi statici.

In `docs/progettazione.md` includi una sezione che mappa esplicitamente *come* ciascuno dei tre punti è stato affrontato.

## 3. Modello di scoring — DUE assi separati (decisione architetturale centrale)

Non un singolo numero opaco. **Due assi distinti**, in cascata:

### Asse 1 — Validità (gate binario, regole dure, NO LLM)
Controlli deterministici: telefono IT valido, email valida / non disposable, nome non gibberish, **dedup** (stesso phone/email negli ultimi N giorni), non spam/test, **consenso presente**. Se il gate fallisce → categoria `invalid`.
- **Distingui due tipi di fallimento:** `invalid-fake` (dato falso/spam → **scartare**) vs `incomplete` (lead legittimo ma manca un campo → **chiedere info mancanti**, NON scartare).
- **Principio di invalidazione conservativa:** in caso di incertezza **non auto-scartare mai** (il `false-invalid`, cioè buttare un compratore vero, è l'errore più costoso del sistema).

### Asse 2 — Qualità (0-100, solo sui lead validi)
Somma di sub-score pesati, tutti calcolati in modo **deterministico**:
- **Intent (0-40):** budget esplicito, modello specifico, permuta, slot test drive, urgenza, finanziamento (i segnali più predittivi in automotive). I campi di intent vengono **estratti dal testo libero `message` dall'LLM** (vedi §4) e poi pesati in modo deterministico.
- **Fit (0-25):** stock/disponibilità del modello, coerenza campagna↔prodotto, copertura geografica (zip nel bacino).
- **Contattabilità (0-20):** completezza/validità contatti, canale, orario.
- **Qualità sorgente (0-15):** prior **data-driven calibrato sullo storico** — tasso di conversione storico per canale/campagna/platform. **Questo è il componente calibrato di rischio.**

**Soglie iniziali (da calibrare sullo storico):** `hot ≥ 75`, `warm 50-74`, `cold 25-49`, gate fallito → `invalid`. Pesi e soglie **non hardcoded per sempre**: sono punti di calibrazione del feedback loop (§11).

## 4. Ruolo dell'LLM (punto critico da difendere)

**L'LLM NON decide lo score.** Fa solo due cose:
1. **Extractor**: dal testo libero `message` estrae i campi di intent (budget, modello, permuta, slot, urgenza, finanziamento) come output strutturato JSON strict → alimentano il sub-score Intent in modo deterministico.
2. **Explainer**: genera la **motivazione sintetica** in linguaggio naturale a partire dallo score già calcolato.

Conseguenze (da scrivere nei doc):
- Lo score resta **deterministico e auditabile** → risposta pronta a **GDPR Art. 22** (decisione automatizzata): logica ispezionabile + motivazione = "diritto di spiegazione" già implementato.
- **Redazione PII prima dell'LLM:** il `message` può contenere nome/telefono; tokenizza/redigi le PII *prima* di passarlo all'extractor (riusa il pattern di tokenizzazione, non solo nei log).
- **Gate sull'uso dell'LLM:** non chiamarlo su lead già `invalid` o su messaggi banali/vuoti → risparmio token e latenza.
- **Upgrade v2 (documentato, NON nel take-home):** gradient boosting (LightGBM/XGBoost) addestrato su storico lead→esito che produce `P(qualifica)` → 0-100, con SHAP per la spiegabilità. La repo consegna la versione **regole + prior calibrato + LLM-extraction**.

## 5. Vincoli e decisioni già prese (vincolanti)

- **Provider LLM: OpenAI**, default **MOCK-FIRST**: la repo **gira senza API key** con mock **deterministici**; la chiamata reale a OpenAI sta dietro un **adapter unico** (`llm.py`) e si attiva *solo* se la key è nell'ambiente. La demo end-to-end deve essere riproducibile con `pip install` + un comando, **senza alcuna chiave**. *(Nota per la prod, da scrivere nei doc: l'adapter è provider-agnostico apposta; per **data-residency EU/GDPR** la scelta di produzione va rivista verso **Amazon Bedrock (regione EU)** o **Azure OpenAI (EU)**, più forti del raw OpenAI API sulla residenza dei dati. Self-host scartato: GPU 24/7 ingiustificate a questo volume.)*
- **Stack: microservizio Python con FastAPI, async-first**, presentato come **add-on del monolite esistente** (Vue.js + backend Java su EKS/Kubernetes su AWS). **Non scrivere Java, non toccare il frontend Vue.** La dashboard esistente leggerà solo nuovi campi (score/priorità/motivazione) sul record già letto.
- **Ingestion via coda/evento (MOCKATA)**: astrazione `queue` in-memory o su file. Il servizio consuma il lead, esegue scoring + azioni agentiche, e scrive il risultato indietro al monolite via **REST callback (mockato)**. **Idempotenza/dedup** sull'ingestion (stesso lead ⇒ non rielaborato due volte; collegato al segnale `is_duplicate`).
- **Endpoint REST sincrono `POST /score`** come via secondaria di ingresso.
- **Tutti i confini esterni MOCKATI dietro interfacce sostituibili** (Protocol/ABC): monolite, coda, canali WhatsApp/email/SMS, enrichment.
- **Invariante duro:** Asse 1 (validità), i pesi dell'Asse 2 e le soglie **non devono mai dipendere dall'output dell'LLM**; l'LLM contribuisce solo estrazione e motivazione, ed è sempre degradabile a un fallback solo-regole.
- **Agentica: UN SINGOLO agente tool-calling con gate HITL**, *non* multi-agente. Gli stage *scorer / enricher / actioner* sono **componenti modulari**, non agenti separati. Multi-agente citato nei doc **solo come evoluzione futura**.
- **Niente over-engineering:** niente DB nel take-home (lo storico mock sta su JSON/CSV), niente vector DB, niente event sourcing, niente microservizi multipli, niente orchestrazione multi-agente. **Semplice ma completo.**

### Pattern da riusare (erano fatti bene nella prima versione)
- **Service layer modulare e tipizzato**; entrypoint sottile che chiama i servizi.
- **LLM dietro un adapter unico** con **prompt centralizzati** e **output strutturato via JSON schema strict**.
- **Privacy-by-design**: PII tokenizzate, dati esposti al modello ristretti a una **whitelist esplicita**, nessun leak verso log/terzi.
- **Degradazione graceful**: ogni dipendenza/input mancante ha un fallback; il sistema non si blocca mai; errori tracciati.

## 6. Struttura della repository (indicativa — adatta con buon senso, non stravolgere)

```
lead-scoring/
├── README.md                      # setup, avvio, demo (italiano)
├── docs/
│   ├── progettazione.md           # Parte 1 architettura + Parte 2 trade-off (italiano)
│   ├── architecture.md            # doc tecnico (italiano)
│   ├── cost_model.md              # modello di costo: formule, stime, ROI + analisi SLA
│   ├── roadmap.md                 # fasi 1-4 (cosa è implementato vs documentato)
│   └── diagrams/                  # diagrammi ASCII e/o immagini
├── requirements.txt | pyproject.toml
├── .env.example                   # OPENAI_API_KEY opzionale, flag MOCK
├── src/
│   ├── main.py                    # entrypoint FastAPI (POST /score, callback, health)
│   ├── config.py                  # settings, feature flag mock-first
│   ├── models/                    # Pydantic: Lead, ScoredLead, RiskResult, AgentAction...
│   ├── services/
│   │   ├── scoring/
│   │   │   ├── validity.py        # Asse 1: gate binario, regole dure, dedup, consenso (NO LLM)
│   │   │   ├── quality.py         # Asse 2: sub-score Intent/Fit/Contattabilità/Sorgente
│   │   │   ├── risk_model.py      # P(invalid), P(conversione), confidenza — calibrato sullo storico
│   │   │   ├── extraction.py      # extractor LLM dei campi intent dal message
│   │   │   └── combiner.py        # categoria + priorità + assemblaggio output spiegabile
│   │   ├── agent/
│   │   │   ├── agent.py           # agent loop tool-calling + HITL gate
│   │   │   └── tools.py           # i 6 tool mockati (vedi §9)
│   │   ├── history.py             # lettura storico / personalizzazione / dedup / prior di conversione
│   │   ├── llm.py                 # adapter LLM unico, mock-first, JSON strict, PII-redaction
│   │   ├── privacy.py             # tokenizzazione PII, whitelist campi
│   │   └── integrations/          # boundary mockati: queue, monolith_callback, channels, enrichment
│   ├── prompts/                   # prompt centralizzati (extractor, explainer)
│   └── pipeline.py                # orchestrazione end-to-end di un lead
├── data/
│   ├── leads_mock.json            # lead correnti (vari canali/piattaforme, casi: hot/warm/cold/invalid/incompleti/duplicati)
│   └── leads_history.json         # storico lead passati CON ESITI (qualifica/conversione) per calibrazione + personalizzazione
├── review_ui/ | cli.py            # dashboard/CLI di review: coda priorizzata + explainability
├── scripts/
│   ├── run_demo.py                # batch mock end-to-end, riproducibile
│   └── evaluate_risk.py           # valutazione offline del modello di rischio sullo storico (metriche + calibrazione)
└── tests/
```

## 7. Requisiti funzionali

Per ogni lead, produci un oggetto strutturato (Pydantic) con **almeno** questi campi:
```json
{
  "lead_id": "…",
  "score": 0,
  "category": "hot | warm | cold | invalid",
  "validity": { "is_valid": true, "failure_type": "none | fake | incomplete", "reasons": ["…"] },
  "quality_breakdown": { "intent": 0, "fit": 0, "contactability": 0, "source_quality": 0 },
  "risk": { "invalid_probability": 0.0, "conversion_probability": 0.0, "confidence": 0.0 },
  "rationale": "motivazione sintetica in linguaggio naturale (generata dall'LLM explainer)",
  "recommended_actions": ["lead valido | chiedere info mancanti | scartare"],
  "personalization": { "is_duplicate": false, "is_returning_customer": false, "history_notes": "…" },
  "agent_actions_taken": [ { "tool": "…", "args": {}, "status": "executed | pending_approval | skipped", "reason": "…" } ],
  "priority": 0,
  "processed_at": "…",
  "latency_ms": 0
}
```

- **Ingestion**: consuma lead da coda mock e da `POST /score`; scrivi il risultato indietro via callback mock al monolite; dedup idempotente.
- **Validità (Asse 1)** e **Qualità (Asse 2)** come da §3; categoria e `priority` derivate in `combiner.py`.
- **Modello di rischio**: calibrato sullo storico mock; produce `P(invalid)`, `P(conversione)`, `confidence`. **Documenta la metodologia** (frequenze condizionate o regressione logistica leggera). `scripts/evaluate_risk.py` stampa metriche sullo storico (es. AUC, precision/recall sulla classe `hot`, **false-invalid rate**, curva di calibrazione score↔conversione).
- **Personalizzazione via storico**: dedup/lead già visto, cliente ricorrente, comportamento passato; influenza score, azioni, priorità e il testo dei messaggi dell'agente.
- **Categorie → azioni:**
  - `hot` → valido + **azione automatica** (conferma test drive via WhatsApp/SMS oppure priorità massima) — **solo se consenso verificato E confidenza alta**, altrimenti HITL. *(È qui che si riduce davvero il costo del call center.)*
  - `warm` → valido, priorità normale per l'operatore.
  - `cold` → **nurturing automatico**, nessuna chiamata operatore.
  - `invalid` → **scartare** se `fake`, oppure **chiedere info mancanti** se solo `incomplete`.
- **Agente tool-calling con HITL**: vedi §9.
- **Review UI/CLI**: mostra la **coda priorizzata** e, per ogni lead, la **spiegazione** (validità, breakdown qualità, rischio, azioni intraprese). È il valore dimostrativo per la demo dal vivo.
- **Demo riproducibile**: uno script che gira il batch mock end-to-end senza chiavi.

## 8. Requisiti non funzionali e KPI

- **Resilienza prima della scala.** Dato il volume basso, il focus è il **fallback sul singolo lead**, non l'autoscaling. Includi comunque una breve **nota di capacità** (i numeri di §1) per mostrare di averla valutata, ma non sovradimensionare.
- **SLA: esito ≤ 2 minuti per lead** (target reale: secondi). In `docs/` un'**analisi latenza** con p50/p95/p99 end-to-end, budget per stage (validità → estrazione LLM → qualità/rischio → azioni), e % entro SLA. La chiamata LLM è nel percorso critico ma gated.
- **Gestione errori (valutata esplicitamente):** timeout/errore LLM → **fallback score solo-regole** (degrada, non blocca lo SLA — possibile *perché* l'LLM non è nel calcolo dello score); **circuit breaker**; retry + backoff; **DLQ** per poison messages; idempotenza/dedup; invalidazione conservativa (mai auto-scartare in incertezza).
- **MODELLO DI COSTO esplicito** (`docs/cost_model.md`) con **formule e numeri**: baseline (10.000 × 4€ = 40.000 €/giorno ≈ 1,2 M€/mese); costo LLM + infra del sistema (parametrico: token/lead, prezzo modello Haiku-class, worker → ~500-1.000 €/mese); risparmio automatizzando hot e deprioritizzando invalid/cold (~360-480 k€/mese, con assunzioni dichiarate); **ROI** e break-even, con tabella/grafico.
- **KPI — 5 famiglie** (coprono i 3 richiesti + qualità modello):
  1. **Business:** riduzione costo CC (€/giorno, %), CPQL, chiamate evitate, conversione per categoria, lead→appuntamento→vendita.
  2. **Qualità modello:** precision/recall classe `hot`, **false-invalid rate** (il rischio più costoso), AUC, calibrazione score↔conversione, drift, concordanza auto↔operatore.
  3. **Latenza/SLA:** p50/p95/p99 e2e, % entro SLA, tasso timeout/fallback.
  4. **Costo infra:** costo per lead (compute + token), ROI mensile.
  5. **Privacy:** % chiamate LLM con PII redatta, data residency EU, retention, tracking consenso, audit log delle decisioni automatiche.

## 9. Agente: tool e gate HITL

Un **singolo** agente tool-calling, con accesso ad almeno questi tool (tutti mockati, interfacce sostituibili):
- `request_missing_info(channel, fields)` — chiede al lead i campi mancanti (caso `incomplete`);
- `enrich_from_history(lead)` — arricchisce dal proprio storico;
- `schedule_test_drive(slot)` — prenota un test drive;
- `send_message(channel, template)` — invia un messaggio su un canale;
- `route_to_operator(priority)` — instrada all'operatore con priorità;
- `mark_invalid(reason)` — marca il lead come invalido (mai sotto incertezza).

Requisiti:
- l'agente ragiona su **lead + score + storico** e decide quali tool invocare e in che ordine (piccola state machine);
- **policy di gating HITL esplicita, motivata da costo/rischio/consenso:** azioni **costose o irreversibili** (es. `send_message`, `schedule_test_drive`) procedono in automatico **solo** su lead `hot` con **consenso verificato E confidenza alta**, altrimenti restano `pending_approval` (conferma nella review UI); azioni a basso rischio (`route_to_operator`) automatiche;
- ogni azione registrata in `agent_actions_taken` con esito e motivazione;
- la logica di scoring/gating dell'Asse 1/2 **non** dipende dall'agente né dall'LLM.

## 10. Mappatura sullo stack di produzione (Java / Vue / EKS / AWS)

Nei doc, spiega come l'add-on Python si innesta in produzione (e quali mock diventano reali):
- **Ingestion async event-driven**: l'app Java pubblica il lead su **SQS** (+ **DLQ**); il consumer di scoring elabora e riscrive lo score → retry/idempotenza "gratis". Sync `POST /score` solo dove serve lo score nella stessa response (non è il caso d'uso principale).
- **LLM**: **Amazon Bedrock (regione EU)**, modello Haiku-class (veloce/economico, dati in AWS → privacy), oppure Azure OpenAI EU. L'adapter rende la sostituzione ovvia.
- **Callback / dashboard**: REST verso il backend **Java** che popola i nuovi campi *score/priorità/motivazione* sul record già letto dalla dashboard **Vue**; **push WebSocket** per riordinare la coda di priorità. Nessuna riscrittura FE.
- **Storage + audit (solo prod)**: score/motivazione/feature su **Postgres (RDS/Aurora)**; storico di training su **S3/Redshift**; **log immutabile** delle decisioni automatiche per GDPR.
- **Deployment**: container del microservizio su **EKS/Kubernetes**; segreti (`OPENAI_API_KEY`/Bedrock) via secret manager.
- **Diagramma ASCII** del flusso end-to-end (ingestion SQS → validità → estrazione LLM → qualità+rischio → agente/azioni → callback → dashboard).

## 11. Feedback loop (ciò che rende il sistema "intelligente" nel tempo)

Documenta (e abbozza nel codice dove ragionevole) il ciclo: **esiti operatore + conversioni reali** → confronto con score/categoria previsti → **ricalibrazione di pesi e soglie** e ri-allenamento periodico del modello di rischio. È il meccanismo che lega lo storico (gap #3) al miglioramento continuo. Nel take-home basta che lo storico mock contenga gli **esiti** e che `evaluate_risk.py` mostri la calibrazione; il loop di produzione va descritto in `docs/roadmap.md`.

## 12. Fasi del progetto (cosa è implementato vs documentato)

- **Fase 1 — MVP / take-home (IMPLEMENTATO):** Asse 1 validità + Asse 2 sub-score a regole + prior di sorgente calibrato + modello di rischio leggero + extractor/explainer LLM (mock-first) → output completo {score, categoria, validità, rischio, motivazione, azioni}. FastAPI, integrazioni esterne mockate, agente con HITL, review UI/CLI, test, README, modello di costo, diagrammi.
- **Fasi 2-4 (DOCUMENTATE, non implementate):** produzione event-driven (SQS/Bedrock/Postgres), modello di rischio ML (gradient boosting + SHAP), automazione outbound completa, feedback loop di ricalibrazione. Vedi `docs/roadmap.md`.

## 13. Documentazione e diagrammi attesi

- **README.md** (IT): cosa fa, prerequisiti, **istruzioni di startup**, come lanciare la demo mock senza chiavi, come attivare OpenAI, come usare la review UI/CLI.
- **docs/progettazione.md** (IT), in due parti:
  - **Parte 1 — Architettura**: panoramica, **mappa dei componenti**, **flusso end-to-end con diagramma ASCII**, *cross-cutting concerns* (privacy, errori, latenza, osservabilità), **tabella dello stack**, e la **sezione che mappa i 3 obiettivi primari**.
  - **Parte 2 — Scelte progettuali e trade-off**: "perché X e non Y" per ogni decisione (due assi vs score unico, LLM extractor-non-scorer, mock-first, FastAPI async, OpenAI ora vs Bedrock-EU in prod, singolo agente vs multi-agente, regole+prior vs ML, no DB nel take-home, invalidazione conservativa).
- **docs/architecture.md** (IT): doc tecnico dettagliato. **docs/cost_model.md**: costo + ROI + analisi SLA. **docs/roadmap.md**: le 4 fasi. Diagrammi ASCII/immagini per architettura e flusso di scoring.

**Importante per la presentazione dal vivo:** dovrò difendere il progetto. **Motiva esplicitamente scelte e trade-off** in `progettazione.md` Parte 2 e tieni il codice leggibile.

## 14. Definition of Done (checklist finale)

- [ ] La demo gira **end-to-end senza API key** (mock-first, output deterministico) con un comando.
- [ ] Esistono i **due dataset mock**: lead correnti multi-canale (con casi hot/warm/cold/invalid/incompleti/duplicati) **e** storico con **esiti**.
- [ ] Per ogni lead l'**oggetto di output completo** (§7), inclusi `validity`, `quality_breakdown`, `risk`+`confidence`.
- [ ] **Due assi**: gate di validità (con distinzione fake vs incomplete) + qualità 0-100 a sub-score; soglie calibrabili.
- [ ] **LLM solo extractor + explainer**, score deterministico/auditabile; PII redatte prima dell'LLM; chiamata LLM gated.
- [ ] **Obiettivo 1 — Agentica**: agente con i 6 tool e **gate HITL** (consenso + confidenza) funzionante, visibile in output e review UI.
- [ ] **Obiettivo 2 — Rischio**: `P(invalid)`/`P(conversione)`+`confidence` **calibrati sullo storico**; `evaluate_risk.py` mostra metriche + calibrazione + false-invalid rate.
- [ ] **Obiettivo 3 — Customer_data**: storico **riletto** per personalizzazione **e** calibrazione; influenza score/azioni/priorità.
- [ ] **Categorie → azioni** corrette (hot auto-azione consenso-gated, cold nurturing, invalid scarta/chiedi-info).
- [ ] **Fallback solo-regole** su errore LLM verificato; circuit breaker / DLQ / idempotenza descritti; invalidazione conservativa.
- [ ] **`POST /score`** sincrono + ingestion via **coda mock** (idempotente) + **callback mock** al monolite.
- [ ] **Review UI/CLI** con coda priorizzata + explainability.
- [ ] **Modello di costo** (formule, ROI, break-even) + **KPI a 5 famiglie** + analisi latenza/SLA.
- [ ] **Mappatura prod** (SQS/DLQ, Bedrock-EU, Postgres/S3, k8s, WebSocket Vue) + **feedback loop** + **roadmap a 4 fasi**.
- [ ] Doc completa in **italiano**, codice in **inglese**; trade-off motivati in `progettazione.md` Parte 2.
- [ ] Niente over-engineering: un solo agente, un solo servizio, nessun DB nel take-home; ML e multi-agente solo come evoluzione futura.

Procedi creando la repository completa. Dove i dettagli minori non sono specificati, usa buon senso ingegneristico e **documenta brevemente la scelta**.
