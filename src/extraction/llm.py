"""Single mock-first LLM adapter for semantic feature extraction.

This is the ONLY place a real LLM is touched, and it does exactly one thing:
turn a (PII-redacted) message into :class:`ExtractedFeatures`. It never assigns
a score.

Three modes:
  * MOCK (default): a deterministic **fixture map** (message -> ExtractedFeatures)
    loaded from ``data/mock_extractions.json``. Honest stand-in for the LLM, with
    NO keyword/regex "fake NLP" and NO hardcoded vehicle catalog. A message not
    in the fixture yields a low-confidence default (offline we cannot truly
    extract), which also exercises the ``low_confidence`` path (§10).
  * OPENAI: used only when enabled + key present + ``openai`` importable. Any
    failure/timeout raises the recoverable :class:`LLMError` so the caller falls
    back to a structured-only default. (Prod: map to Amazon Bedrock EU, §9.)
  * Auto-degrade: a small circuit breaker trips to MOCK after repeated failures.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from src.config import Settings, get_settings
from src.extraction.prompts import EXTRACTION_JSON_SCHEMA, build_extraction_messages
from src.models.lead import ExtractedFeatures

logger = logging.getLogger(__name__)

try:  # pragma: no cover - exercised only when openai is installed
    from openai import OpenAI  # type: ignore

    _OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore
    _OPENAI_AVAILABLE = False


class LLMError(Exception):
    """Recoverable LLM failure (timeout, API error, bad output)."""


_CB_THRESHOLD = 3  # trip to mock after this many consecutive OpenAI failures.


def _normalize_message(text: str | None) -> str:
    """Normalization key for the fixture map: lowercase, single-spaced, stripped."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().lower())


def _low_confidence_default() -> ExtractedFeatures:
    """Honest 'the mock has no opinion' result for an unknown message.

    No semantic signals; flagged low-confidence so the score relies on the
    deterministic structured features only.
    """
    return ExtractedFeatures(
        extraction_source="mock",
        extraction_confidence=0.2,
        rationale_signals="(mock offline) nessun segnale semantico per questo messaggio",
    )


class LLMAdapter:
    """Mock-first adapter exposing a single ``extract`` method."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._consecutive_failures = 0
        self._client: Any | None = None
        self._fixture: dict[str, dict[str, Any]] = self._load_fixture()

        self._use_openai = (
            self._settings.llm_mode == "openai"
            and bool(self._settings.openai_api_key)
            and _OPENAI_AVAILABLE
        )
        if self._use_openai:
            try:  # pragma: no cover - requires openai + key
                self._client = OpenAI(api_key=self._settings.openai_api_key)
            except Exception:  # noqa: BLE001
                self._client = None
                self._use_openai = False

    # -- fixture (mock) ------------------------------------------------------

    def _load_fixture(self) -> dict[str, dict[str, Any]]:
        path: Path | None = self._settings.mock_extractions_path
        if path is None or not Path(path).exists():
            return {}
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        by_message = raw.get("by_message", {}) if isinstance(raw, dict) else {}
        return {_normalize_message(k): v for k, v in by_message.items()}

    @property
    def mode(self) -> str:
        """Effective mode after availability/circuit-breaker resolution."""
        return "openai" if self._openai_active() else "mock"

    def _openai_active(self) -> bool:
        return (
            self._use_openai
            and self._client is not None
            and self._consecutive_failures < _CB_THRESHOLD
        )

    # -- public --------------------------------------------------------------

    def extract(
        self, redacted_message: str, context: dict | None = None
    ) -> ExtractedFeatures:
        """Extract features from an already-redacted message.

        Mock mode resolves deterministically (fixture or low-confidence default).
        OpenAI mode raises :class:`LLMError` on any failure so the caller can
        fall back to a structured-only default.
        """
        if not self._openai_active():
            return self._mock_extract(redacted_message)

        try:  # pragma: no cover - requires live OpenAI
            features = self._openai_extract(redacted_message, context)
        except LLMError:
            self._consecutive_failures += 1
            raise
        except Exception as exc:  # noqa: BLE001
            self._consecutive_failures += 1
            raise LLMError(f"OpenAI extraction failed: {exc}") from exc

        self._consecutive_failures = 0
        return features

    def complete_json(
        self, system: str, messages: list[dict], schema: dict, model: str | None = None
    ) -> dict[str, Any]:
        """Reusable structured-output call returning a schema-validated JSON object.

        Used by the agent's LLM planner (off the hot path). ``model`` selects the
        deployment and defaults to the extraction model; the agent passes its own
        ``openai_agent_model`` so the two calls can run on different tiers.
        Mock-first like :meth:`extract`: with no live OpenAI backend it raises
        :class:`LLMError` so the caller degrades deterministically (there is no
        offline "planner mock" -- in mock mode the deterministic planner is used
        directly). Shares the same circuit breaker as extraction.
        """
        if not self._openai_active():
            raise LLMError("complete_json has no offline backend (use mock planner).")

        try:  # pragma: no cover - requires live OpenAI
            full_messages = [{"role": "system", "content": system}, *messages]
            response = self._client.chat.completions.create(  # type: ignore[union-attr]
                model=model or self._settings.openai_model,
                messages=full_messages,
                temperature=0,
                timeout=self._settings.llm_timeout_s,
                response_format={"type": "json_schema", "json_schema": schema},
            )
            content = response.choices[0].message.content
            if not content:
                raise LLMError("Empty OpenAI completion response.")
            data = json.loads(content)
        except LLMError:
            self._consecutive_failures += 1
            raise
        except Exception as exc:  # noqa: BLE001
            self._consecutive_failures += 1
            raise LLMError(f"OpenAI completion failed: {exc}") from exc

        self._consecutive_failures = 0
        return data

    def _mock_extract(self, redacted_message: str) -> ExtractedFeatures:
        data = self._fixture.get(_normalize_message(redacted_message))
        if data is None:
            return _low_confidence_default()
        payload = {k: v for k, v in data.items() if not k.startswith("_")}
        payload["extraction_source"] = "mock"
        return ExtractedFeatures(**payload)

    def _openai_extract(  # pragma: no cover - requires live OpenAI
        self, redacted_message: str, context: dict | None
    ) -> ExtractedFeatures:
        messages = build_extraction_messages(redacted_message, context)
        response = self._client.chat.completions.create(  # type: ignore[union-attr]
            model=self._settings.openai_model,
            messages=messages,
            temperature=0,
            timeout=self._settings.llm_timeout_s,
            response_format={
                "type": "json_schema",
                "json_schema": EXTRACTION_JSON_SCHEMA,
            },
        )
        content = response.choices[0].message.content
        if not content:
            raise LLMError("Empty OpenAI extraction response.")
        try:
            data = json.loads(content)
        except (ValueError, TypeError) as exc:
            raise LLMError(f"Invalid JSON from OpenAI: {exc}") from exc
        data["extraction_source"] = "llm"
        return ExtractedFeatures(**data)
