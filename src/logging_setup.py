"""Structured, PII-safe logging setup for the lead-scoring pipeline.

The pipeline logs per-lead progress for observability, but raw contact data
(phone, email, the free-text message, names) must NEVER reach the logs. This
module provides:

  * ``configure_logging`` : idempotently install a structured handler on the
    package logger and return it;
  * a redacting ``logging.Filter`` that scrubs any raw email/phone pattern that
    slips into a log record's message, as a defensive backstop.

Callers should still pass only safe identifiers (``lead_id``, category, score,
opaque tokens). The redaction filter is a safety net, not a license to log PII.
"""

from __future__ import annotations

import logging
import re

# Package logger name; all pipeline modules log under this namespace.
LOGGER_NAME = "lead_scoring"

# Defensive redaction patterns (mirror src.privacy, kept local so this module
# has no import-time dependency on the privacy module).
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s.\-]?){8,}\d(?!\d)")

_EMAIL_TOKEN = "[EMAIL]"
_PHONE_TOKEN = "[PHONE]"

# Compact, parseable line format (key fields are emitted by callers via the
# message itself; this stays human-readable for the demo CLI).
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


class _RedactingFilter(logging.Filter):
    """Scrub raw email/phone patterns from formatted log messages.

    Acts on the already-interpolated ``record.getMessage()`` so it also catches
    PII embedded in ``args``. This is a backstop: code should not log PII at all.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging break the pipeline
            return True
        redacted = _EMAIL_RE.sub(_EMAIL_TOKEN, message)
        redacted = _PHONE_RE.sub(_PHONE_TOKEN, redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure and return the package logger (idempotent).

    Installs a single stream handler with the structured format and the PII
    redaction filter. Repeated calls do not stack handlers; they only adjust
    the level.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    # Don't propagate to the root logger (avoid duplicate, unfiltered output).
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        handler.addFilter(_RedactingFilter())
        logger.addHandler(handler)
    else:
        for handler in logger.handlers:
            if not any(isinstance(f, _RedactingFilter) for f in handler.filters):
                handler.addFilter(_RedactingFilter())

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger under the package namespace (PII-safe handlers)."""
    if name:
        return logging.getLogger(f"{LOGGER_NAME}.{name}")
    return logging.getLogger(LOGGER_NAME)
