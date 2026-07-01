"""Deterministic scoring zone: shared feature vector + linear scorer."""

from src.scoring.feature_vector import build_feature_vector
from src.scoring.scorer import compute_score, top_contributions
from src.scoring.weights import load_thresholds, load_weights

__all__ = [
    "build_feature_vector",
    "compute_score",
    "top_contributions",
    "load_weights",
    "load_thresholds",
]
