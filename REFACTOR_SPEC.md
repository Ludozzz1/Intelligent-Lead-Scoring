# Spec di refactoring — Lead Scoring deterministico + Lead-Resolution Agent

> Documento di lavoro per Claude Code. Obiettivo: ristrutturare questo repo verso
> un'architettura a **due zone** (hot path deterministico + un'unica zona agentica),
> con tutte le integrazioni esterne **mockate**. È un take-home: conta la qualità
> architetturale, la spiegabilità e il rispetto dei vincoli (latenza, costo, privacy),
> non la completezza di produzione.

---

## 0. Come usare questo documento (istruzioni operative per Claude Code)

Prima di modificare qualunque cosa:

1. **Fai l'inventario del repo esistente.** Elenca i moduli/cartelle attuali, cosa fa
   ciascuno, cosa è riusabile e cosa va riorganizzato. Produci un breve report.
2. **Proponi un piano di migrazione** (mapping: file attuale → posizione target in §4)
   **prima** di spostare/riscrivere in blocco. Aspetta conferma per i cambi distruttivi.
3. **Refactor incrementale, non rewrite.** Preserva la logica valida già scritta;
   riallineala alla struttura target. Non cancellare codice funzionante senza motivo.
4. **Mocka ogni integrazione esterna** (LLM, SMS/WhatsApp, calendario dealer, inventory,
   stima permuta, DB) dietro interfacce, così il core gira end-to-end in locale.
5. **Testa man mano.** Ogni componente del hot path deve avere almeno un test che
   dimostra il comportamento atteso su un lead di esempio.
6. Quando una scelta non è ovvia o tocca dati personali/azioni verso l'utente, **chiedi**
   invece di assumere.

Questo documento descrive il **target**. Dove confligge con codice esistente sensato,
segnalalo e proponi, non sovrascrivere silenziosamente.

---

## 1. Contesto e obiettivo

Riceviamo ~**10.000 lead/giorno** da web e campagne paid (Meta, ecc.). Ogni lead ha campi
strutturati + un messaggio testuale libero. Esempio:

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

Per ogni lead il sistema deve produrre:

- **score** 0–100
- **categoria**: `hot | warm | cold | invalid`
- **motivazione** sintetica
- **azione consigliata**: `lead_valido | chiedere_info | scartare`
- e, dove sensato, **eseguire azioni automatiche / interagire con l'utente**

**Vincoli/KPI da rispettare (sono criteri di valutazione):**

- **Latenza**: esito di scoring entro **2 minuti** (oltre, il tasso di qualifica crolla).
- **Costo**: call center €4/chiamata; lo scoring serve a evitare chiamate inutili. La
  pipeline LLM deve costare un ordine di grandezza meno del risparmio.
- **Privacy**: lead italiani, PII (nome, telefono, email) → GDPR. Minimizzazione + residency EU.
- **Volume**: 10k/giorno, con possibili burst.

Stack di riferimento (per il design, non da replicare interamente nel mock): AWS,
EKS/Kubernetes, dashboard Vue.js + Java. Per il repo: linguaggio a scelta coerente con
l'esistente; il design deve restare cloud-agnostic ma mappabile su AWS (SQS, Bedrock EU, ecc.).

---

## 2. Principi guida (NON negoziabili)

1. **Deterministic-first.** Tutto ciò che può essere una regola **è** una regola.
   L'LLM si usa solo dove il dominio è davvero aperto (testo libero, conversazione).
2. **Un'unica call LLM nel hot path**: la **structured extraction**. Lo scoring,
   la categoria e l'azione consigliata sono deterministici.
3. **Niente LLM-as-judge nel hot path.** Uno score calcolato da regole è corretto per
   costruzione: non si verifica con un secondo LLM. La QA è asincrona, su campione (§6).
4. **Bounded agency.** Esiste **una sola** zona agentica (§7), fuori dal percorso a SLA,
   con tool, guardrail, budget di passi e fallback a human handoff.
5. **Spiegabilità e auditabilità.** Ogni score deve poter mostrare i contributi delle
   feature. Ogni azione automatica deve lasciare traccia.
6. **Fail-fast + fallback deterministico.** Se l'LLM non risponde entro timeout, si scora
   sui soli campi strutturati e si consegna comunque entro lo SLA, flaggato `low_confidence`.
7. **PII minimization.** All'LLM non va il telefono/email in chiaro se non necessario:
   manda `phone_valid: true`, non il numero. Endpoint in regione EU.

---

## 3. Architettura a due zone (overview)

```
                          ┌──────────────────────── HOT PATH (deterministico, ≤2 min) ───────────────────────┐
   lead ──> [ingest] ──> [validation gate] ──fail──> categoria=invalid, azione=scartare  (STOP, niente LLM)
                              │ pass
                              ▼
                        [extraction]  ← UNICA call LLM (output JSON validato)  ──timeout──> fallback deterministico
                              │
                              ▼
                        [scoring]  ← deterministico, pesi/soglie da calibrazione offline (§6)
                              │
                              ▼
                        [categorization]  ← bande sullo score
                              │
                              ▼
                        [motivation]  ← derivata dall'extraction (no extra call)
                              │
                              ▼
                        [action decision]  ← deterministico: lead_valido | chiedere_info | scartare
                              │
   ───────────────────────────┼──────────────────────────────────────────────────────────────────────────────┘
                              │  (solo per i lead che lo meritano: hot/warm incompleti o negoziabili)
                              ▼
                   ┌──────────── ZONA AGENTICA (asincrona, event-driven, NON a SLA di scoring) ──────────────┐
                   │  Lead-Resolution Agent: loop a passi variabili con tool, stato persistito, guardrail.    │
                   │  Obiettivo: portare il lead a uno stato terminale (booked / info completate / handoff /  │
                   │  disqualified-no-response). Recupero info mancanti E negoziazione appuntamento = stesso   │
                   │  loop, traiettorie diverse.                                                               │
                   └───────────────────────────────────────────────────────────────────────────────────────────┘

   [OFFLINE / batch]  Calibration & validation pipeline (§6): usa lo storico (qui sintetico)
                      per fissare pesi e soglie e per il backtest/ROI. NON è nel runtime.
```

Lo **SLA dei 2 minuti riguarda lo score**, non l'interazione con l'utente. L'agente vive
*dopo* lo scoring e su orizzonte lungo (l'utente può rispondere dopo minuti/ore o mai).

---

## 4. Struttura target del repository

Adatta i nomi alle convenzioni del linguaggio esistente; questa è la mappa logica.

```
.
├── README.md                      # startup, architettura, diagrammi, costi (richiesto dalla consegna)
├── REFACTOR_SPEC.md               # questo documento
├── src/
│   ├── ingestion/                 # parsing/normalizzazione del lead in arrivo
│   ├── gate/                      # validation gate fail-fast (deterministico, pre-LLM)
│   ├── extraction/                # UNICA call LLM: prompt + schema + validazione output
│   ├── scoring/                   # scorer deterministico (consuma pesi calibrati)
│   ├── categorization/            # bande score -> hot/warm/cold (invalid è gestito dal gate)
│   ├── motivation/                # genera la motivazione dai segnali estratti
│   ├── action/                    # decisione deterministica della next-action
│   ├── agent/                     # Lead-Resolution Agent (state machine, loop)
│   │   ├── state_machine.*        # stati, eventi, transizioni
│   │   ├── policy.*               # selezione del prossimo tool dato lo stato
│   │   ├── guardrails.*           # max turni/messaggi, timeout, consent gating
│   │   └── tools/                 # tool MOCKATI (vedi §7)
│   ├── calibration/               # pipeline offline (§6): label, fit, backtest, ROI
│   │   ├── synthetic_history.*    # generatore di storico sintetico con esiti
│   │   ├── calibrate.*            # soglie + logistic regression -> artifact pesi/soglie
│   │   └── backtest.*             # precision/recall hot + stima risparmio
│   ├── pipeline/                  # orchestrazione del hot path (compone i moduli sopra)
│   ├── integrations/              # client esterni dietro interfacce (LLM, SMS, calendar...)
│   │   └── mocks/                 # implementazioni mock deterministiche
│   ├── models/                    # schemi/typed models: Lead, ExtractedFeatures, ScoreResult, AgentState
│   └── config/                    # soglie, pesi calibrati (artifact), feature flags
├── data/
│   └── synthetic/                 # storico sintetico generato (per la calibrazione/backtest)
├── docs/
│   ├── architecture.md            # diagramma a due zone, decisioni
│   ├── cost_model.md              # modello di costo/ROI parametrico
│   └── decision_rights.md         # matrice diritti di decisione dell'agente
└── tests/
```

---

## 5. Hot path — zona deterministica

### 5.1 Ingestion & validation gate (fail-fast, PRIMA dell'LLM)

Scopo: scartare a costo zero i lead inutilizzabili **prima** di spendere una call LLM.
Tutto deterministico. Se il gate fallisce → `categoria=invalid`, `action=scartare`, STOP.

Check (esempi, configurabili):

- **Contatto**: telefono normalizzabile/plausibile (formato IT), email sintatticamente valida.
- **Dedup**: stesso telefono/email già presente di recente → flag duplicato.
- **Spam/gibberish ovvi**: messaggio vuoto/non testo, pattern di bot.
- **Campi minimi presenti**: almeno un canale di contatto valido.

Output del gate: `passed: bool`, `invalid_reasons: []`. Nota: l'invalidità è **un gate**,
non una banda di score. Lo score non viene calcolato per gli invalid.

### 5.2 Feature extraction (UNICA call LLM)

Una sola chiamata. Modello piccolo/veloce, endpoint EU. Output **JSON validato contro
schema** (rifiuta/ritenta se non conforme). **Minimizza i PII** nel prompt (manda il
messaggio e i flag di validità, non il numero di telefono).

Schema di output di riferimento (`ExtractedFeatures`):

```json
{
  "budget_value_eur": 35000,
  "budget_present": true,
  "vehicle_model_mentioned": "SUV ibrido / Toyota C-HR",
  "vehicle_specificity": "specific | generic | none",
  "trade_in_present": true,
  "trade_in_vehicle": "VW Golf 2018",
  "urgency_signals": ["disponibile test drive sabato"],
  "intent_strength": "high | medium | low",
  "availability_mentioned": true,
  "sentiment": "positive | neutral | negative",
  "missing_critical_fields": ["timeline_acquisto"],
  "looks_invalid": false,
  "extraction_confidence": 0.0,
  "rationale_signals": "lead con budget chiaro, permuta e disponibilità immediata"
}
```

Il campo `rationale_signals` alimenta la motivazione (§5.5) senza una seconda call.
`looks_invalid` può promuovere a `invalid` un lead che ha passato il gate ma risulta
chiaramente non valido dal testo (gibberish, fuori area).

### 5.3 Scoring (deterministico)

Calcola lo score 0–100 come **combinazione lineare interpretabile** del vettore di feature.
I **pesi sono appresi offline** da un modello supervisionato (§6) e salvati come artifact;
in assenza di artifact, lo scorer usa **pesi naive** euristici come fallback (vedi §6).

> **Feature-building condivisa (requisito anti-skew).** Deve esistere **una sola** funzione
> deterministica `build_feature_vector(ExtractedFeatures, structured_fields) -> vector`,
> usata **identica** in due momenti: (a) in training, sullo storico, per costruire la matrice
> di feature; (b) a runtime, sull'output dell'estrazione live. Il modello apprende e scora
> **sullo stesso vettore**, mai sul testo grezzo. Questa simmetria evita il *training/serving
> skew*: è un punto su cui si viene interrogati, va reso esplicito nel codice.

Feature di scoring (esempi, input di `build_feature_vector`):

- budget presente/coerente, specificità veicolo, permuta presente, intent_strength,
  urgency/availability, sentiment;
- **raggiungibilità** (telefono/email validi) — un ottimo lead non contattabile non è "hot";
- **recency** (`created_at`): un lead vecchio vale meno operativamente;
- match geografico city/zip ↔ dealer.

Requisito: lo score deve esporre i **contributi per feature** (per spiegabilità e dashboard).
A runtime è un prodotto scalare: microsecondi, nessun rischio SLA.

### 5.4 Categorization (bande)

`hot | warm | cold` per bande sullo score. `invalid` **non** è una banda: arriva dal gate
(§5.1) o da `looks_invalid` (§5.2). **Le soglie non sono arbitrarie**: provengono dalla
calibrazione (§6), tarate sul tasso di conversione storico. Le soglie stanno in `config/`.

### 5.5 Motivation

Frase sintetica derivata da `rationale_signals` + contributi di score. Niente call extra.
Deve essere leggibile dall'operatore ("budget chiaro, permuta, disponibilità immediata → alto intent").

### 5.6 Action decision (deterministico)

Mappa stato → azione consigliata:

- `invalid` → `scartare`
- valido ma con `missing_critical_fields` rilevanti → `chiedere_info` (può attivare l'agente)
- valido e completo → `lead_valido` (o `nurturing` per i cold completi)

Questa decisione è anche il **trigger** verso la zona agentica (§7), ma solo per i lead che
lo meritano e solo quando un'azione puramente deterministica non basta.

**Contratto operatore (deterministico, `src/action/suggestions.py`).** Oltre alla
`recommended_action`, ogni lead porta tre campi pensati per la dashboard del call center:

- `queue` — bucket di coda: `attiva` (l'operatore deve chiamare) · `agente`
  (auto-gestito, nessuna chiamata) · `scartato`. **`scartare` è una disposizione di
  sistema**: gli `invalid` escono dalla coda di chiamata attiva e finiscono in un elenco
  "scartati" solo auditabile (motivazione conservata) — non sono un task per l'operatore.
- `next_best_action` — il "cosa fare ora" da un **vocabolario chiuso** specifico per la
  concessionaria (test drive, permuta, finanziamento, conferma modello, info mancanti). È
  il **mirror del tool belt dell'agente** (§7.3): senza consenso lo fa l'operatore, con
  consenso lo fa l'agente (e il suggerimento diventa "non chiamare"). Nessuna seconda call
  LLM: è funzione deterministica delle feature già estratte. Il *perché* resta in `motivation`.
- `agent_status` — etichetta operatore dello stato dell'agente, valorizzata a fine sessione.

**Riallineamento post-agente (`finalize_with_session`).** L'agente è decoupled (§7): lo
`ScoredLead` esce entro SLA, e quando la sessione si risolve il lead viene **riallineato**
all'esito reale — su `recommended_action`, `next_best_action`, `queue`, `agent_status` e
sullo score/categoria/priorità arricchiti (re-scoring §7.2, persistito in
`AgentSession.final_score`). I lead che richiedono un umano **ri-emergono** nella coda attiva:

| AgentState finale | queue | recommended_action | significato operatore |
|---|---|---|---|
| `BOOKED` | agente | `lead_valido` | prenotato, nessuna chiamata |
| `NURTURED` | agente | `nurturing` | asset inviato, nessuna chiamata |
| `COMPLETED_INFO` | attiva | `lead_valido` | info recuperate → chiama, qualificato |
| `PENDING_APPROVAL` | attiva | `lead_valido` | approva la prenotazione predisposta |
| `HANDOFF_HUMAN` | attiva | `lead_valido` | riprendi tu e chiama |
| `DISQUALIFIED_NO_RESPONSE` | attiva | `lead_valido` | ultimo tentativo manuale o chiudi |

---

## 6. Calibrazione su dati storici — modello supervisionato per i pesi (pipeline OFFLINE)

> Principio chiave: **lo storico non è nel runtime.** È una pipeline batch che addestra un
> modello supervisionato e produce un **artifact** (pesi + soglie) consumato dallo scorer
> deterministico. Per il take-home gira su **storico sintetico**: pipeline vera, dati finti.
>
> **Approccio.** I **pesi dello scorer sono i coefficienti di un modello supervisionato
> interpretabile** (logistic regression) addestrato sui lead passati. Input del modello =
> il **vettore di `build_feature_vector`** (la stessa funzione del runtime, §5.3). Target =
> la **label di esito** (il lead ha portato a un risultato finito, vedi sotto). I **pesi
> naive** euristici restano come **fallback** quando non c'è un artifact addestrato (primo
> avvio, dati insufficienti, o per confronto/baseline nel backtest).

Componenti in `src/calibration/`:

1. **Definizione della label (Stadio 0).** Variabile target binaria esplicita estratta
   dall'esito storico: es. `qualified` o `appointment_booked`. Documenta la scelta.
   "Hot" = alta P(label).
2. **`synthetic_history`**: genera N lead mock con feature plausibili **e un esito** (label)
   correlato in modo controllato, così l'addestramento e la calibrazione sono dimostrabili.
3. **Costruzione della matrice di training (anti-skew).** Per ogni lead storico: ricava
   `ExtractedFeatures` (se lo storico ha già i campi strutturati, usali; **estrai con l'LLM
   solo i lead che non li hanno, una volta sola in batch** — non ri-estrarre tutto a ogni
   training) → applica **la stessa `build_feature_vector` del runtime** → ottieni il vettore.
   Risultato: matrice `X` (vettori) + vettore `y` (label).
4. **`calibrate`** — due output complementari:
   - **Pesi (supervisionato)**: fit di una **logistic regression** su `(X, y)`. I
     coefficienti, normalizzati su 0–100, diventano i pesi dello scorer. **Niente black-box**
     (gradient boosting/DNN): l'explainability è un KPI: devi poter mostrare il contributo
     di ogni feature. Output: `config/score_weights.json`.
   - **Soglie**: replay dei lead nello scorer (coi pesi appresi), raggruppa per fascia di
     score, calcola il tasso di conversione per fascia, fissa i tagli hot/warm/cold dove il
     tasso scalina. Output: `config/category_thresholds.json`.
5. **Fallback naive**: un set di pesi euristici hardcoded (`config/score_weights_naive.json`)
   usato quando manca l'artifact appreso, e come **baseline** contro cui confrontare il
   modello nel backtest (il modello deve battere la baseline per giustificarsi).
6. **`backtest`**: su un holdout, misura **precision/recall di "hot"**, confronta
   modello vs pesi naive, e stima **risparmio call center vs revenue persa** (per ROI/presentazione).

**Catena completa (training vs runtime — devono combaciare):**

```
TRAINING (offline):  storico → ExtractedFeatures (già presenti o estratti 1 volta in batch)
                              → build_feature_vector → X ; label → y
                              → fit logistic → coefficienti = pesi → artifact

RUNTIME (online):    lead → estrazione LLM → ExtractedFeatures
                              → build_feature_vector → vettore
                              → prodotto scalare coi pesi appresi → score
```

**Caveat da scrivere nel codice/README (sono parte della qualità della proposta):**

- **No leakage**: usa solo feature disponibili all'arrivo del lead. Niente esiti come input
  (es. "numero di chiamate fatte" è un outcome, non una feature).
- **Selection bias**: la label storica è in parte funzione della policy operativa passata
  (lead lavorati prima convertono di più anche per quello). Va **dichiarato**; soluzione
  pulita = holdout/randomizzazione → lavoro futuro.
- **No overfitting** su dati sintetici: l'obiettivo è la **metodologia** (label → fit →
  backtest → monitoring), non l'AUC.

---

## 7. Zona agentica — Lead-Resolution Agent

> **Un solo agente.** Obiettivo: portare un lead promettente a uno **stato terminale**.
> "Recupero info mancanti" e "negoziazione appuntamento" sono la **stessa** macchina,
> con traiettorie diverse. La prenotazione **non** è un secondo agente: è un'**azione
> terminale**. Mandare un template con link **non** è agentico; lo diventa solo con
> **negoziazione** (slot proposti/contro-proposti, deviazioni dell'utente).

### 7.1 Trigger — routing allineato al valore (rev.)

> **Aggiornamento rispetto alla v0.** Il trigger originale ("solo `hot`/`warm` con
> interazione aperta; `invalid`/`cold` mai") privilegiava la *forma* del lead, non il
> suo *valore*. La regola attuale aggancia il trigger al **valore + consenso**, in linea
> con l'obiettivo di business "gestire in automatico i lead di alta qualità". Sorgente
> di verità: [src/action/decision.py](src/action/decision.py) (`decide_action` +
> `route_complete`).

Il **consenso è valutato a monte**: senza consenso l'agente non può messaggiare, quindi
il lead va all'operatore invece di attivare un goal che finirebbe subito in handoff.

- `invalid` → **scarto** (mai agente).
- **Incompleto** (`missing_critical_fields`, a **qualsiasi** banda incluso `cold`) + consenso
  → goal `recover_info`: l'agente recupera le info e poi **ri-score** (§7.2). Senza consenso
  → l'operatore chiede le info.
- **Completo** + consenso, **automation-worthy** (`hot`, oppure `warm` con `score ≥ warm_high`)
  → goal `negotiate_appointment` (booking proattivo, **non** serve più `availability_mentioned`).
- **Completo** `warm` medio/basso, o senza consenso → **operatore** (prioritizzato).
- **Completo** `cold` (debole) + consenso → goal `nurturing` (un asset automatico, **nessuna
  chiamata**); senza consenso → bassa priorità/drop.

L'agente **non** disqualifica mai per qualità (l'`invalid` resta il gate deterministico): un
cold ancora debole dopo l'arricchimento va in **nurturing**, non in `invalid`. Può chiudere
per **non-risposta**.

### 7.2 State machine + re-scoring (riferimento)

Stati: `TRIGGERED → AWAITING_USER_REPLY → EVALUATING_REPLY → {PROPOSING_SLOT →
AWAITING_CONFIRMATION → PENDING_APPROVAL → BOOKED} | COMPLETED_INFO | NURTURED |
HANDOFF_HUMAN | DISQUALIFIED_NO_RESPONSE | TERMINATED`.

Event-driven: ogni risposta in arrivo dell'utente ri-attiva il loop. **Stato persistito**
(DB/coda); l'agente si "sveglia" sugli eventi, non è request-response sincrono. La
prenotazione passa da `PENDING_APPROVAL` (azione in *stage*, non eseguita) e si esegue
solo su un evento `HUMAN_APPROVAL` dell'operatore (§7.5).

**Loop di arricchimento (re-scoring async).** Dopo una risposta a un `recover_info`, l'agente
`re_extract`-a la risposta, **fonde** le feature (`merge_features`) e **ri-calcola**
`score`/`category` riusando la **stessa** `build_feature_vector`/`semantic_values` sul vettore
strutturale cachato a trigger-time (nessun training/serving skew, nessuna re-lettura del lead,
**nessuna call LLM extra** oltre al `re_extract` già previsto). Poi **ri-instrada** con la
stessa `route_complete`: completo e booking-worthy → prosegue al booking nello stesso wake;
warm medio → `COMPLETED_INFO` (operatore); ancora incompleto → continua a chiedere (bounded dal
budget messaggi) o consegna il lead arricchito all'operatore. Tutto **fuori SLA**.

### 7.3 Tool (mockati — firme di riferimento)

```
re_extract(message) -> ExtractedFeatures          # rianalizza la risposta dell'utente
check_availability(dealer_id, preferences) -> [slot]
check_inventory(vehicle) -> {in_stock, alternatives}
recommend_alternatives(vehicle, budget) -> {alternatives}   # se out-of-stock
estimate_trade_in(vehicle_desc) -> {range_eur}    # qualifica e "scalda" (permuta)
simulate_financing(price, down, trade_in) -> {monthly_eur}  # leva sul budget
send_message(channel, text) -> {sent, message_id}  # gated da consenso (§7.5)
send_asset(vehicle, asset_type) -> {sent}          # scheda/listino (consent-gated)
capture_consent(lead_id) -> {sent}                 # double opt-in (acquisisce il consenso)
schedule_followup(lead_id, when) -> {followup_id}  # ladder prima della disqualifica
update_crm(lead_id, outcome, note) -> {delivered}  # writeback esito agente
warm_transfer_to_operator(lead_id, context) -> {ticket_id}
book_appointment(dealer_id, slot, lead_id) -> {confirmed, appointment_id}  # human-approval
escalate_to_human(reason, lead_id) -> {ticket_id}
```

A ogni passo la **policy** (il *planner*, §7.6) sceglie il tool che fa avanzare verso
lo stato terminale, in base allo stato corrente e all'ultima risposta utente.

### 7.4 Stop conditions & guardrail

- `max_turns` / `max_messages` per lead (anti-spam, budget di passi).
- `response_timeout`: se l'utente non risponde entro X → `DISQUALIFIED_NO_RESPONSE`.
- Incertezza dell'agente sopra soglia o richiesta fuori scope → `HANDOFF_HUMAN`.
- **Fallback sempre disponibile**: se un tool fallisce o l'LLM è giù → handoff umano,
  mai loop infiniti.

### 7.5 Matrice dei diritti di decisione (in `docs/decision_rights.md`)

| Azione                              | Autorità in v1            |
|-------------------------------------|---------------------------|
| Chiedere info via messaggio         | **auto** (solo con consenso marketing verificato) |
| Proporre uno slot                   | **auto**                  |
| Confermare la prenotazione          | **human-approval** (v1)   |
| Stima permuta / mostrarla all'utente| auto (range indicativo)   |
| Disqualificare per qualità          | **mai** all'agente (resta il gate deterministico) |
| Qualunque contenuto marketing-like  | gated dal consenso        |
| Escalation a operatore              | auto                      |

### 7.6 Planner (deterministico / LLM) — "l'LLM propone, il deterministico dispone"

La policy che sceglie il prossimo tool è un **planner** astratto
([src/agent/planner.py](src/agent/planner.py)):

- `DeterministicPlanner` — **default in `llm_mode=mock`**: traduce 1:1 le traiettorie
  (keyword matching). Comportamento invariato, test offline senza chiave.
- `LLMPlanner` — orchestrazione conversazionale via tool-calling
  (`LLMAdapter.complete_json`), **fuori dallo SLA** (zona async). **Non** è
  "LLM-as-judge" (vietato §11): quello vieta l'LLM nello *scoring*; qui è solo
  scelta del prossimo passo. Su errore/timeout/output invalido → **degrade** al
  planner deterministico (mai blocco).

**Invariante non negoziabile**: ogni decisione del planner passa da `enforce()`
([guardrails.py](src/agent/guardrails.py)), unico chokepoint che applica la matrice
§7.5 + allow-list + consenso + budget. L'LLM **propone**, il deterministico
**dispone** (esegue / blocca / mette in *stage* per approvazione umana). La
prenotazione è *staged* in `PENDING_APPROVAL` ed eseguita solo su `HUMAN_APPROVAL`.

---

## 8. Gestione SLA, errori, resilienza

- **Coda** in ingresso (mappabile su SQS) → worker (EKS) con autoscaling per i burst.
- **Timeout duro sull'LLM** (es. 10s): superato → **fallback deterministico** (score sui
  soli campi strutturati, flag `low_confidence`), così lo SLA non dipende mai dall'LLM.
- **Retry** con backoff sull'extraction prima del fallback; **idempotenza** per lead_id
  (riprocessare lo stesso lead non duplica azioni/messaggi).
- Errori dei tool dell'agente → handoff umano, mai stato inconsistente.
- Logging strutturato + audit trail di ogni score e ogni azione automatica.

---

## 9. Privacy / PII

- **Minimizzazione verso l'LLM**: invia il testo del messaggio e flag (`phone_valid`,
  `email_valid`), non i PII grezzi quando non servono allo scoring.
- **Residency EU** per qualunque chiamata LLM (mappabile su Bedrock EU).
- **Consent gating**: nessun messaggio automatico senza base di consenso verificata.
- Pseudonimizzazione/segregazione dei PII nello storage; accesso minimo.

---

## 10. Strategia di mock

- Tutte le integrazioni esterne dietro **interfacce** in `src/integrations/`, con impl.
  mock **deterministiche** in `integrations/mocks/`.
- **LLM mock**: dato un messaggio noto, restituisce un `ExtractedFeatures` plausibile
  (mappa input→output deterministica, eventualmente con un seed). Deve permettere di
  testare anche il path `looks_invalid` e `low_confidence`.
- **Canale messaggi / calendar / inventory / trade-in**: mock con risposte fisse e casi
  d'errore simulabili (per testare i guardrail e l'handoff).
- Lo **storico sintetico** in `data/synthetic/` alimenta la calibrazione e il backtest.
- Il sistema deve girare **end-to-end in locale** senza credenziali reali.

---

## 11. Anti-pattern da EVITARE (NON fare)

- ❌ LLM-as-judge nel hot path per "verificare" lo score.
- ❌ Più di una call LLM nel hot path (motivazione/azione come call separate).
- ❌ Agente o loop non deterministico **dentro** il percorso a SLA / ad alto volume.
- ❌ Score black-box (gradient boosting/DNN) che uccide l'explainability.
- ❌ Soglie di categoria scelte a intuito invece che calibrate.
- ❌ `invalid` trattato come banda di score invece che come gate.
- ❌ Storico usato nel runtime (retrieval/"lead simili" online).
- ❌ PII grezzi mandati all'LLM senza necessità.
- ❌ Routing dealer/operatore via LLM (è una regola deterministica).
- ❌ Far disqualificare lead all'agente per "qualità".

> Nota: il **planner LLM dell'agente** (§7.6) **non** viola questi punti — vive nella
> zona agentica **async, fuori dallo SLA**, **non** scora né giudica lo score (orchestra
> solo la conversazione) e ogni sua azione passa da `enforce()` deterministico.

---

## 12. Fuori scope per la v1 (citare, non costruire)

- Agente-SDR conversazionale end-to-end (massimo rischio brand/compliance).
- Re-engagement automatico di lead raffreddati (alto rischio normativo; dietro consenso).
- Monitoring/drift online e ricalibrazione automatica (citato in §6, non implementato).

---

## 13. Definition of done

- [ ] Hot path end-to-end su un lead di esempio, con **una sola** call LLM (mock), entro lo SLA.
- [ ] Gate fail-fast funzionante (lead invalid scartati senza LLM).
- [ ] Scorer deterministico con **contributi per feature** esposti, alimentato da
      `build_feature_vector` **condivisa con il training** (no training/serving skew).
- [ ] Categorie da **soglie calibrate** (artifact letto da `config/`).
- [ ] Pipeline di calibrazione con **modello supervisionato (logistic regression)** che
      apprende i pesi dallo storico, **pesi naive come fallback/baseline**, backtest che li
      confronta, eseguibile su storico sintetico, con caveat (leakage, selection bias) documentati.
- [ ] Lead-Resolution Agent con state machine, tool mockati, guardrail, handoff e fallback.
- [ ] Matrice diritti di decisione in `docs/`.
- [ ] Fallback deterministico su timeout/outage LLM.
- [ ] PII minimization verso l'LLM + nota residency EU.
- [ ] README con startup, diagramma a due zone e modello di costo/ROI.
- [ ] Anti-pattern di §11 assenti dal codice.
