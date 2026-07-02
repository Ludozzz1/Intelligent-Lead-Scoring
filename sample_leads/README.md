# sample_leads — lead dimostrativi (due per banda)

Set curato per mostrare tutte le categorie in un colpo solo. **Nessuno è nel fixture
mock** (`data/mock_extractions.json`): in `llm_mode=openai` l'estrazione è reale.

## Bande verificate (run reale, `gpt-5.4-mini` estrazione)

| lead_id | categoria | score | agent_goal | perché |
|---------|-----------|------:|------------|--------|
| SHOW-HOT | hot | 100 | negotiate_appointment | intento alto + budget 35k + modello preciso + permuta + disponibilità sabato |
| SHOW-WARM-HIGH | warm (alto) | 64 | recover_info | modello + budget 38k, ma niente urgenza/disponibilità |
| SHOW-WARM-LOW | warm (basso) | 50 | recover_info | modello preciso ma **budget assente** |
| SHOW-COLD | cold | 20 | — | richiesta generica, nessun budget/modello, intento basso |
| SHOW-INVALID | invalid | 0 | scartare | telefono `1111111111` (bogus) + email `@mailinator.com` (usa-e-getta) → gate |

> Nota: con l'LLM reale lo score oscilla di qualche punto tra run (non deterministico
> anche a `temperature=0`); le bande restano stabili perché i segnali sono netti.
> `SHOW-INVALID` è invece deterministico al 100% (bocciato dal gate strutturale,
> `src=skipped`, non arriva nemmeno all'LLM).

## Secondo set (`*-2`) — una variante per banda

Stessi profili di segnale dei 5 originali (auto/città/nomi diversi), così le bande
attese coincidono. Gli score puntuali **non sono verificati** con un run reale: si
attendono nella stessa banda perché i segnali sono i medesimi.

| lead_id | banda attesa | segnali (perché) |
|---------|--------------|------------------|
| SHOW-HOT-2 | hot | modello preciso + budget 30k + permuta + disponibilità domani + urgenza |
| SHOW-WARM-HIGH-2 | warm (alto) | modello + budget 40k, ma niente urgenza/disponibilità |
| SHOW-WARM-LOW-2 | warm (basso) | modello preciso ma **budget assente** |
| SHOW-COLD-2 | cold | richiesta generica, nessun budget/modello, intento basso |
| SHOW-INVALID-2 | invalid | telefono `0123456789` (sequenza → bogus) + email `@guerrillamail.com` (usa-e-getta) → gate |

## File

- `SHOW-HOT.json`, `SHOW-WARM-HIGH.json`, `SHOW-WARM-LOW.json`, `SHOW-COLD.json`,
  `SHOW-INVALID.json` (+ le varianti `SHOW-*-2.json`) — **un lead per file** (singolo
  oggetto JSON), caricabili uno alla volta dalla dashboard ("Carica un lead" → file
  uploader).
- `leads_showcase.json` — tutti e 10 i lead in **un array**, per gli strumenti CLI che
  vogliono una lista (`cli.py --data`, `agent_repl.py --data`).

## Come usarli

Upload singolo dalla dashboard:
```
./.venv/Scripts/python.exe -m streamlit run streamlit_app.py
```
poi "Carica un lead" e seleziona es. `sample_leads/SHOW-HOT.json`.

Tabella di scoring dell'intero set (5 estrazioni, nessun agente):
```
./.venv/Scripts/python.exe cli.py --data sample_leads/leads_showcase.json
```

Dettaglio + traiettoria agente su un lead:
```
./.venv/Scripts/python.exe cli.py --detail SHOW-HOT --data sample_leads/leads_showcase.json
```

Conversazione dal vivo con l'agente (risposte libere — vedi `docs/agent_repl_playbook.md`):
```
./.venv/Scripts/python.exe scripts/agent_repl.py --data sample_leads/leads_showcase.json --lead SHOW-HOT
```
