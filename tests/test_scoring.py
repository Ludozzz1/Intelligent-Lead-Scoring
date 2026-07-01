"""Scoring: shared feature vector (anti-skew), linear scorer, weights loader."""

from __future__ import annotations

import json

from src.config import Settings
from src.models.lead import ExtractedFeatures
from src.scoring.feature_vector import build_feature_vector
from src.scoring.scorer import compute_score, top_contributions
from src.scoring.weights import load_weights
from tests.conftest import NOW, make_lead

_EXPECTED_FEATURES = {
    "intent_strength", "budget_present", "vehicle_specificity", "trade_in_present",
    "availability", "sentiment", "reachability", "recency", "geo_match",
}


def _rich_features() -> ExtractedFeatures:
    return ExtractedFeatures(
        budget_present=True, vehicle_specificity="specific", trade_in_present=True,
        availability_mentioned=True, intent_strength="high", sentiment="positive",
        extraction_confidence=0.9, extraction_source="mock",
    )


def test_feature_vector_has_exactly_the_53_features():
    fv = build_feature_vector(_rich_features(), make_lead(), now=NOW)
    assert set(fv.values) == _EXPECTED_FEATURES


def test_feature_values_normalized_unit_interval():
    fv = build_feature_vector(_rich_features(), make_lead(), now=NOW)
    assert all(0.0 <= v <= 1.0 for v in fv.values.values())


def test_reachability_mobile_vs_email_vs_none():
    mobile = build_feature_vector(_rich_features(), make_lead(), now=NOW)
    email = build_feature_vector(_rich_features(), make_lead(phone=None), now=NOW)
    none = build_feature_vector(_rich_features(), make_lead(phone=None, email=None), now=NOW)
    assert mobile.values["reachability"] == 1.0
    assert email.values["reachability"] == 0.6
    assert none.values["reachability"] == 0.0


def test_geo_match_milan_in_catchment():
    fv = build_feature_vector(_rich_features(), make_lead(zip_code="20148"), now=NOW)
    assert fv.values["geo_match"] == 1.0
    far = build_feature_vector(_rich_features(), make_lead(zip_code="90133"), now=NOW)
    assert far.values["geo_match"] == 0.1


def test_recency_decays_with_age():
    fresh = build_feature_vector(
        _rich_features(), make_lead(created_at=NOW), now=NOW
    )
    from datetime import datetime
    old = build_feature_vector(
        _rich_features(), make_lead(created_at=datetime(2026, 1, 1)), now=NOW
    )
    assert fresh.values["recency"] > old.values["recency"]


def test_anti_skew_same_function_drives_score():
    # Same build_feature_vector used here is the runtime one; rich -> high score.
    fv = build_feature_vector(_rich_features(), make_lead(), now=NOW)
    result = compute_score(fv, _rich_features())
    assert result.score >= 85
    assert result.weights_source == "naive"


def test_contributions_sum_to_score():
    fv = build_feature_vector(_rich_features(), make_lead(), now=NOW)
    result = compute_score(fv, _rich_features())
    assert result.score == max(0, min(100, round(sum(result.contributions.values()))))


def test_empty_features_score_low():
    feats = ExtractedFeatures(extraction_source="skipped")
    fv = build_feature_vector(feats, make_lead(phone=None, email=None), now=NOW)
    result = compute_score(fv, feats)
    assert result.score < 30


def test_low_confidence_propagates_to_result():
    feats = ExtractedFeatures(extraction_source="fallback")
    fv = build_feature_vector(feats, make_lead(), now=NOW)
    result = compute_score(fv, feats)
    assert result.low_confidence is True


def test_top_contributions_orders_by_impact():
    fv = build_feature_vector(_rich_features(), make_lead(), now=NOW)
    result = compute_score(fv, _rich_features())
    top = top_contributions(result, 3)
    assert len(top) == 3
    assert top[0][1] >= top[1][1] >= top[2][1]


def test_learned_weights_take_precedence(tmp_path):
    (tmp_path / "score_weights.json").write_text(
        json.dumps({"weights": {"intent_strength": 100}}), encoding="utf-8"
    )
    settings = Settings(config_dir=tmp_path)
    weights, source = load_weights(settings)
    assert source == "learned"
    assert weights == {"intent_strength": 100.0}
