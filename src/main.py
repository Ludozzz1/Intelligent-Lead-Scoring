"""FastAPI entry point exposing the scoring pipeline over HTTP.

Three endpoints, all thin:

* ``POST /score``    - score one :class:`Lead` synchronously and return the full
  :class:`ScoredLead` (delegates to the cached pipeline; never raises out).
* ``POST /callback`` - mock receiver standing in for the Java monolith: it
  records the delivered :class:`ScoredLead` and returns the same ack the real
  ``MockMonolithCallback`` would produce.
* ``GET  /health``   - liveness + a small snapshot (effective LLM mode, whether
  the history dataset loaded, app version).

The heavy lifting (history, LLM adapter, scoring) lives in the pipeline; this
module only adapts it to a request/response boundary. Run with::

    py -3 -m uvicorn src.main:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from src.integrations.monolith_callback import MockMonolithCallback
from src.models.lead import Lead
from src.models.output import ScoredLead
from src.pipeline import get_pipeline

APP_VERSION = "0.1.0"

app = FastAPI(
    title="Lead Scoring Service",
    version=APP_VERSION,
    description=(
        "Deterministic, mock-first lead scoring for an automotive call center. "
        "The score is a linear combination over a shared feature vector; the "
        "single LLM call only produces the extracted semantic features."
    ),
)

# Single in-memory monolith receiver shared across /callback requests so the
# delivered acks can be inspected (mirrors the demo's round-trip).
_monolith = MockMonolithCallback()


@app.post("/score", response_model=ScoredLead)
def score(lead: Lead) -> ScoredLead:
    """Score one lead synchronously and return the complete result.

    The pipeline is a total function: any internal failure degrades to a safe
    ``invalid`` result rather than surfacing a 5xx.
    """
    return get_pipeline().score_lead(lead)


@app.post("/callback")
def callback(scored_lead: ScoredLead) -> dict:
    """Mock monolith receiver: record the scored lead and acknowledge it.

    Stands in for ``POST /leads/{id}/score`` on the Java monolith. Returns the
    delivery ack (status, lead_id, minimal non-PII payload).
    """
    return _monolith.send_score(scored_lead)


@app.get("/health")
def health() -> dict:
    """Liveness probe plus a small runtime snapshot for ops dashboards."""
    pipeline = get_pipeline()
    return {
        "status": "ok",
        "llm_mode": pipeline._llm.mode,  # effective mode after availability checks
        "history_loaded": pipeline.history.record_count > 0,
        "version": APP_VERSION,
    }
