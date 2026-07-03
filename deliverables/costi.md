# Previsione di costo

Quanto costa far girare lo scoring e quanto fa risparmiare al call center. I numeri
di costo LLM sono **misurati** con `scripts/estimate_cost.py` (chiamate reali,
`response.usage`).

## Baseline

Il call center oggi, potenzialmente, chiama tutti i lead. Nel caso peggiore, quindi:

```
10.000 lead/giorno × 4 €/chiamata = 40.000 €/giorno   (≈ 1,2 M€/mese)
```

Lo scoring prova ad abbassare questa cifra su due fronti: **scarta** gli `invalid` (fuori dalla
coda) e **auto-gestisce** con l'agente i lead di valore con consenso. Il suo costo — da chiamate API a LLM — 
è potenzialmente molto inferiore.

## Cosa misura lo script

`scripts/estimate_cost.py` passa un campione di ~30 lead per la pipeline **reale** e
legge i token effettivi da ogni risposta OpenAI:

- **Estrazione** (hot path): 1 call per lead valido, modello `gpt-5.4-mini`.
  Riporta token in/out medi e **€ per call**.
- **Agente** (off-SLA): sui soli lead che triggerano, guida le sessioni reali col
  planner `gpt-5.4` e conta call e token → **€ per sessione**.

Requisiti: `OPENAI_API_KEY` + `LLM_MODE=openai`. Output a console + `cost_report.json`.

## Come si scala al volume reale

Dal costo per-call misurato al costo giornaliero:

```
costo_estrazione/giorno = 10.000 × quota_lead_che_estraggono × €/call_estrazione
costo_agente/giorno      = 10.000 × quota_lead_triggerati    × €/sessione_agente
costo_LLM/giorno         = costo_estrazione/giorno + costo_agente/giorno
```

```
risparmio_lordo/giorno = 10.000 × quota_chiamate_evitate × 4 €
   dove chiamate_evitate = invalid scartati + lead auto-gestiti dall'agente
risparmio_netto/giorno = risparmio_lordo/giorno − costo_LLM/giorno
ROI = risparmio_lordo / costo_LLM
```

## Risultati misurati

Run reale su **26 lead** di `data/leads_mock.json` (da `cost_report.json`). I **token sono la misura reale**; i valori in € sono calcolati tramite i prezzi attuali delle API di openai.

| Voce | Valore |
|---|---|
| Token estrazione in/out (medi per call) | 972 / 132 |
| **€ per call di estrazione** | €0,000225 |
| Call planner medie per sessione | 3,5 |
| **€ per sessione agente** | €0,0138 |
| Quota lead che triggerano l'agente (campione) | 50% (13/26) |
| **Costo LLM totale / giorno** | €70,88 |
| Costo LLM totale / mese | €2.126 |
| Chiamate evitate / giorno | 6.923 (69%) |
| **Risparmio lordo / giorno** | €27.692 |
| Risparmio netto / giorno | €27.621 |
| **ROI** | ~391× |

## Note

Bisogna considerare sia che è improbabile che il call center chiami tutte le lead della giornata, sia che il numero di lead del giorno che triggerano il giro dell'agente sia elevato quanto questo test. La valutazione avviene, infatti, su un gruppo di lead mockup ricco di informazioni, spesso molto diverso dalle lead reali. 
