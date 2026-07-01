# Calibrazione dei pesi e delle soglie (metodologia)

Riferimento: REFACTOR_SPEC §6. **Il training dei pesi è fuori scope per questo
repo** (scelta esplicita): consegniamo lo scorer con **pesi naive** come sorgente
attiva e l'**architettura pronta** a ricevere pesi appresi senza toccare il
codice. Questo documento descrive *come* si otterrebbero i pesi "veri".

## Stato attuale (cosa è implementato)

- **Pesi naive** in [config/score_weights_naive.json](../config/score_weights_naive.json):
  combinazione lineare tarata a mano sulle feature §5.3. Sono il **fallback attivo**.
- **Soglie naive** in [config/category_thresholds.json](../config/category_thresholds.json).
- Il loader [src/scoring/weights.py](../src/scoring/weights.py) preferisce un
  artifact appreso `config/score_weights.json` se presente, altrimenti il naive.
  → per attivare pesi appresi basta **deporre il file**, nessuna modifica al codice.

## Come si otterrebbero i pesi "veri" (pipeline offline, non a runtime)

Lo storico **non è nel runtime** (§11): la calibrazione è una pipeline batch che
produce un artifact consumato dallo scorer.

1. **Label (Stadio 0).** Variabile target binaria dall'esito storico, già presente
   in [data/leads_history.json](../data/leads_history.json): `qualified` oppure
   `converted`. "Hot" = alta P(label). Documentare la scelta.
2. **Matrice di training (anti-skew).** Per ogni lead storico: ricavare le
   `ExtractedFeatures` (estrazione LLM **una volta sola** in batch sui soli lead
   senza campi) → applicare **la stessa** `build_feature_vector`
   ([src/scoring/feature_vector.py](../src/scoring/feature_vector.py)) usata a
   runtime → vettore. Risultato: matrice `X` + vettore label `y`.
   - *Questa simmetria è il punto chiave*: modello e runtime scoraggiano sullo
     **stesso vettore**, mai sul testo grezzo → niente *training/serving skew*.
3. **Fit (interpretabile).** **Logistic regression** su `(X, y)`. I coefficienti,
   normalizzati su 0–100, diventano i pesi → `config/score_weights.json`.
   Niente black-box (gradient boosting/DNN): l'explainability è un KPI.
4. **Soglie.** Replay dei lead nello scorer coi pesi appresi, raggruppamento per
   fascia di score, calcolo del tasso di conversione per fascia, tagli hot/warm/cold
   dove il tasso scalina → `config/category_thresholds.json`.
5. **Backtest.** Su holdout: precision/recall di "hot", confronto **modello vs
   pesi naive** (il modello deve battere la baseline), stima risparmio call center
   vs revenue persa (ROI).

```
TRAINING (offline):  storico → ExtractedFeatures → build_feature_vector → X ; label → y
                              → fit logistic → coefficienti = pesi → artifact
RUNTIME (online):    lead → estrazione LLM → ExtractedFeatures → build_feature_vector
                              → prodotto scalare coi pesi → score
```

## Caveat (parte della qualità della proposta)

- **No leakage**: solo feature disponibili all'arrivo del lead. Nessun esito come
  input (es. "n. chiamate fatte" è un outcome, non una feature).
- **Selection bias**: la label storica dipende in parte dalla policy operativa
  passata (lead lavorati prima convertono di più anche per quello). Va
  **dichiarato**; soluzione pulita = holdout/randomizzazione → lavoro futuro.
- **No overfitting** su dati sintetici: l'obiettivo è la **metodologia**
  (label → fit → backtest → monitoring), non l'AUC.
