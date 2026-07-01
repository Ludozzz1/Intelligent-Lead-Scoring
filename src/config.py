"""Application settings (pydantic-settings, .env-backed).

Only runtime knobs live here. Scoring weights and category thresholds are NOT
hardcoded settings: they are JSON artifacts under ``config/`` (naive shipped,
learned optional) read by ``scoring/weights.py`` -- so the score is calibratable
without code or env changes (REFACTOR_SPEC §5.3/§5.4).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = parent of the "src" package directory (this file lives in src/).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DATA_DIR = _REPO_ROOT / "data"
_DEFAULT_CONFIG_DIR = _REPO_ROOT / "config"


class Settings(BaseSettings):
    """Runtime configuration loaded from environment and the .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM adapter (mock-first; real provider only with key + llm_mode) ---
    llm_mode: str = "mock"
    openai_api_key: str | None = None
    # Two independent deployments (env: OPENAI_MODEL / OPENAI_AGENT_MODEL):
    #  - openai_model: the hot-path extraction call (Task 1, latency-bound).
    #  - openai_agent_model: the off-SLA agentic planner (Task 2, quality-bound).
    openai_model: str = "gpt-5.4-mini"
    openai_agent_model: str = "gpt-5.4"
    # Hard timeout for the single hot-path LLM call; on breach -> deterministic
    # fallback so the 2-minute SLA never depends on the LLM (§8).
    llm_timeout_s: float = 8.0

    # --- Data locations ---
    data_dir: Path = _DEFAULT_DATA_DIR
    leads_mock_path: Path | None = None
    leads_history_path: Path | None = None
    # Fixture map (message/lead -> ExtractedFeatures) backing the offline mock.
    mock_extractions_path: Path | None = None

    # --- Calibration artifacts (config/) ---
    config_dir: Path = _DEFAULT_CONFIG_DIR
    score_weights_path: Path | None = None  # learned weights (optional)
    score_weights_naive_path: Path | None = None  # shipped fallback weights
    category_thresholds_path: Path | None = None
    dealer_catchment_path: Path | None = None
    blocklists_path: Path | None = None

    # --- Dedup / personalization ---
    dedup_window_days: int = 30

    # --- Agent guardrails (§7.4) ---
    agent_max_turns: int = 6
    agent_max_messages: int = 4
    # Follow-up ladder before disqualifying a non-responder (LLM planner only).
    agent_max_followups: int = 2
    # Per-session ceiling on planner LLM calls (cost guardrail; agent is off-SLA).
    agent_max_llm_calls: int = 8

    def model_post_init(self, __context: object) -> None:
        """Resolve default file paths relative to data_dir / config_dir."""
        if self.leads_mock_path is None:
            self.leads_mock_path = self.data_dir / "leads_mock.json"
        if self.leads_history_path is None:
            self.leads_history_path = self.data_dir / "leads_history.json"
        if self.mock_extractions_path is None:
            self.mock_extractions_path = self.data_dir / "mock_extractions.json"
        if self.score_weights_path is None:
            self.score_weights_path = self.config_dir / "score_weights.json"
        if self.score_weights_naive_path is None:
            self.score_weights_naive_path = self.config_dir / "score_weights_naive.json"
        if self.category_thresholds_path is None:
            self.category_thresholds_path = self.config_dir / "category_thresholds.json"
        if self.dealer_catchment_path is None:
            self.dealer_catchment_path = self.config_dir / "dealer_catchment.json"
        if self.blocklists_path is None:
            self.blocklists_path = self.config_dir / "blocklists.json"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
