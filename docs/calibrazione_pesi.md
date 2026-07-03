# Calibrazione dei pesi

Come si passa dai pesi **naive** attuali a pesi **appresi** dai dati storici. Il
training è **fuori dallo scope** di questa consegna: qui c'è la metodologia, non
l'implementazione.

## Stato attuale

Lo score è una combinazione lineare di 9 feature (vedi `flusso_valutazione.md`). I pesi
attivi sono in `config/score_weights_naive.json` — **hand-tuned**, non addestrati:
scelti in modo che l'ordinamento dei lead sia sensato, ma non ricavati da dati.

Il loader `src/scoring/weights.py` è già pronto per il modello appreso: cerca prima
`config/score_weights.json` (pesi appresi, `source="learned"`), e solo se manca usa i
naive. **Quando arriveranno i pesi addestrati, si sostituisce quel file e basta**.

## Il metodo

**1. Etichetta (label).** Per ogni lead storico si definisce l'esito binario da
predire: convertito / non convertito (es. lead → appuntamento fissato, o → vendita,
secondo la definizione di business). È la `y`.

**2. Feature (X).** Si ricostruiscono le feature con la **stessa `build_feature_vector`**
usata a runtime, applicata ai lead storici. Questo è il punto chiave: niente
**training/serving skew**, perché offline e online il vettore è identico, prodotto dalla
stessa funzione. Serve la stessa estrazione semantica sui messaggi storici (LLM in
batch, offline, senza vincolo di SLA).

**3. Modello: classificazione.** 

- è **lineare e interpretabile**: i coefficienti *sono* i pesi delle feature, stessa
  forma dello scorer attuale (`Σ peso·valore`);
- restituisce una **probabilità di conversione** calibrata, che si mappa direttamente
  sullo score 0–100 e sulle soglie;
- è robusta con poche feature (9) e tanti esempi, e resta un modello semplice da
  comprendere — coerente col principio "score non black-box".

I coefficienti appresi (normalizzati sulla stessa scala) diventano il nuovo
`config/score_weights.json`.

**4. Soglie.** Con le probabilità del modello sul set storico si rifittano le bande
`hot / warm / cold` e la soglia di automazione `warm_high` **massimizzando il tradeoff
conversione/costo**: dove conviene auto-gestire (agente), dove far chiamare l'operatore,
dove scartare. Anche le soglie stanno in `config/` (`category_thresholds.json`), quindi
si aggiornano come i pesi.
