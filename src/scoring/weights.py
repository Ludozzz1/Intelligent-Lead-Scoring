"""Loaders for the scoring artifacts under ``config/`` (with fallback semantics).

* ``load_weights``    -> prefer the learned ``score_weights.json`` (NOT shipped;
  training is out of scope, see docs/calibration.md); otherwise the naive
  ``score_weights_naive.json`` (the active fallback). Returns (weights, source).
* ``load_thresholds`` -> hot/warm/cold bands from ``category_thresholds.json``.
* ``load_catchment``  -> dealer geo catchment from ``dealer_catchment.json``.
* ``load_blocklists`` -> structural gate blocklists from ``blocklists.json``.

All loaders are cached by path and degrade to sane built-in defaults if a file
is missing/unreadable, so the system always runs.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.config import Settings, get_settings

# Built-in defaults (last-resort fallback if even the naive artifact is missing).
_DEFAULT_WEIGHTS: dict[str, float] = {
    "intent_strength": 18,
    "budget_present": 15,
    "reachability": 15,
    "vehicle_specificity": 12,
    "availability": 10,
    "trade_in_present": 8,
    "geo_match": 8,
    "recency": 7,
    "sentiment": 7,
}
_DEFAULT_THRESHOLDS: dict[str, float] = {
    "hot": 72,
    "warm": 45,
    "cold": 25,
    "warm_high": 62,
}
_DEFAULT_CATCHMENT: dict[str, list[str]] = {
    "home_zip_prefixes": ["20"],
    "adjacent_zip_prefixes": ["21", "22", "23", "24", "27", "28"],
    "home_cities": ["milano"],
    "adjacent_cities": ["monza", "como", "varese", "lecco", "bergamo", "pavia", "novara"],
}
_DEFAULT_BLOCKLISTS: dict[str, list[str]] = {"disposable_email_domains": []}


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not Path(path).exists():
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


@lru_cache(maxsize=8)
def _load_weights_cached(learned: str, naive: str) -> tuple[tuple[tuple[str, float], ...], str]:
    learned_data = _read_json(Path(learned))
    if learned_data and isinstance(learned_data.get("weights"), dict):
        return tuple(learned_data["weights"].items()), "learned"
    naive_data = _read_json(Path(naive))
    if naive_data and isinstance(naive_data.get("weights"), dict):
        return tuple(naive_data["weights"].items()), "naive"
    return tuple(_DEFAULT_WEIGHTS.items()), "naive"


def load_weights(settings: Settings | None = None) -> tuple[dict[str, float], str]:
    """Return ``(weights, source)`` where source is "learned" or "naive"."""
    s = settings or get_settings()
    items, source = _load_weights_cached(
        str(s.score_weights_path), str(s.score_weights_naive_path)
    )
    return {k: float(v) for k, v in items}, source


@lru_cache(maxsize=8)
def _load_thresholds_cached(path: str) -> tuple[tuple[str, float], ...]:
    data = _read_json(Path(path))
    thr = data.get("thresholds") if data else None
    if isinstance(thr, dict):
        # float, not int: keeps calibrated/fractional cutoffs intact. The band
        # cutoffs are whole numbers, so 65 == 65.0 keeps the score comparisons correct.
        return tuple((k, float(v)) for k, v in thr.items())
    return tuple(_DEFAULT_THRESHOLDS.items())


def load_thresholds(settings: Settings | None = None) -> dict[str, float]:
    """Return the category bands + the ``warm_high`` automation cutoff (0-100 score)."""
    s = settings or get_settings()
    return dict(_load_thresholds_cached(str(s.category_thresholds_path)))


@lru_cache(maxsize=8)
def _load_catchment_cached(path: str) -> str:
    data = _read_json(Path(path))
    return json.dumps(data) if data else json.dumps(_DEFAULT_CATCHMENT)


def load_catchment(settings: Settings | None = None) -> dict[str, list[str]]:
    """Return the dealer geo catchment (zip prefixes / cities)."""
    s = settings or get_settings()
    data = json.loads(_load_catchment_cached(str(s.dealer_catchment_path)))
    return {
        k: [str(x).strip().lower() for x in v]
        for k, v in data.items()
        if isinstance(v, list)
    }


@lru_cache(maxsize=8)
def _load_blocklists_cached(path: str) -> str:
    data = _read_json(Path(path))
    return json.dumps(data) if data else json.dumps(_DEFAULT_BLOCKLISTS)


def load_blocklists(settings: Settings | None = None) -> dict[str, list[str]]:
    """Return the structural gate blocklists (e.g. disposable email domains)."""
    s = settings or get_settings()
    data = json.loads(_load_blocklists_cached(str(s.blocklists_path)))
    return {
        k: [str(x).strip().lower() for x in v]
        for k, v in data.items()
        if isinstance(v, list)
    }
