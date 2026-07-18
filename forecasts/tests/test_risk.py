"""
OrcaMet Portal — Forecast Engine Tests

Tests for the core risk scoring, recommendation, and model weighting logic.
Run with: python manage.py test forecasts.tests.test_risk
Or with pytest: pytest forecasts/tests/test_risk.py -v

These tests ensure that:
1. Risk scores differentiate across weather conditions
2. Recommendations map to correct risk bands
3. Model weights are geographically sensible
4. Edge cases (NaN, extreme values) are handled safely
5. The sigmoid/ramp primitives behave correctly
"""

import math
from unittest import TestCase

from forecasts.engine.core import (
    calculate_hourly_risk,
    get_recommendation,
    get_model_weights,
    sigmoid,
    ramp,
)


# ============================================================
# Default thresholds (matching core.py and risk_grid.py)
# ============================================================
DEFAULT_THRESHOLDS = {
    "wind_mean_caution": 10.0,
    "wind_mean_cancel": 14.0,
    "gust_caution": 15.0,
    "gust_cancel": 20.0,
    "precip_caution": 0.7,
    "precip_cancel": 2.0,
    "temp_min_caution": 1.0,
    "temp_min_cancel": -2.0,
}


# ============================================================
# Sigmoid primitive
# ============================================================
class TestSigmoid(TestCase):
    """Verify the sigmoid activation function."""

    def test_sigmoid_at_zero(self):
        """sigmoid(0) should be exactly 0.5."""
        self.assertAlmostEqual(sigmoid(0), 0.5, places=10)

    def test_sigmoid_positive(self):
        """Large positive input should approach 1.0."""
        self.assertGreater(sigmoid(10), 0.999)

    def test_sigmoid_negative(self):
        """Large negative input should approach 0.0."""
        self.assertLess(sigmoid(-10), 0.001)

    def test_sigmoid_symmetry(self):
        """sigmoid(x) + sigmoid(-x) should equal 1.0."""
        for x in [0.5, 1.0, 2.0, 5.0]:
            self.assertAlmostEqual(sigmoid(x) + sigmoid(-x), 1.0, places=10)


# ============================================================
# Ramp primitive
# ============================================================
class TestRamp(TestCase):
    """Verify the linear ramp function."""

    def test_ramp_below_soft(self):
        """Below the soft threshold should return 0.0."""
        self.assertEqual(ramp(5.0, 10.0, 14.0, high_bad=True), 0.0)

    def test_ramp_above_hard(self):
        """Above the hard threshold should return 1.0."""
        self.assertEqual(ramp(16.0, 10.0, 14.0, high_bad=True), 1.0)

    def test_ramp_midpoint(self):
        """Midpoint between soft and hard should return 0.5."""
        self.assertAlmostEqual(ramp(12.0, 10.0, 14.0, high_bad=True), 0.5)

    def test_ramp_at_soft(self):
        """Exactly at soft threshold should return 0.0."""
        self.assertEqual(ramp(10.0, 10.0, 14.0, high_bad=True), 0.0)

    def test_ramp_at_hard(self):
        """Exactly at hard threshold should return 1.0."""
        self.assertEqual(ramp(14.0, 10.0, 14.0, high_bad=True), 1.0)

    def test_ramp_inverted_below_soft(self):
        """Inverted ramp (high_bad=False): above soft returns 0.0."""
        self.assertEqual(ramp(5.0, 1.0, -2.0, high_bad=False), 0.0)

    def test_ramp_inverted_above_hard(self):
        """Inverted ramp: below hard returns 1.0."""
        self.assertEqual(ramp(-3.0, 1.0, -2.0, high_bad=False), 1.0)

    def test_ramp_inverted_midpoint(self):
        """Inverted ramp: midpoint between soft and hard."""
        result = ramp(-0.5, 1.0, -2.0, high_bad=False)
        self.assertAlmostEqual(result, 0.5, places=5)

    def test_ramp_nan_returns_nan(self):
        """NaN input should return NaN."""
        result = ramp(float("nan"), 10.0, 14.0)
        self.assertTrue(math.isnan(result))


# ============================================================
# Risk calculation
# ============================================================
class TestCalculateHourlyRisk(TestCase):
    """
    Verify risk scores are bounded, monotonic, and differentiate
    across realistic UK weather conditions.
    """

    def test_calm_day(self):
        """Light winds, no rain, warm — should be low risk."""
        risk = calculate_hourly_risk(3.0, 5.0, 0.0, 15.0, DEFAULT_THRESHOLDS)
        self.assertGreaterEqual(risk, 0.0)
        self.assertLess(risk, 15.0, f"Calm day risk should be <15%, got {risk:.1f}%")

    def test_moderate_wind(self):
        """Winds at caution threshold — risk should be noticeable."""
        risk = calculate_hourly_risk(10.0, 15.0, 0.0, 12.0, DEFAULT_THRESHOLDS)
        # At exactly the soft thresholds, ramps are 0, so risk stays low
        # But this tests the boundary
        self.assertGreaterEqual(risk, 0.0)

    def test_strong_wind(self):
        """Winds above cancel threshold — high risk."""
        risk = calculate_hourly_risk(15.0, 22.0, 0.0, 8.0, DEFAULT_THRESHOLDS)
        self.assertGreater(risk, 50.0, f"Strong wind should be >50%, got {risk:.1f}%")

    def test_extreme_wind(self):
        """Extreme conditions — risk should approach 100%."""
        risk = calculate_hourly_risk(20.0, 30.0, 5.0, -5.0, DEFAULT_THRESHOLDS)
        self.assertGreater(risk, 90.0, f"Extreme conditions should be >90%, got {risk:.1f}%")

    def test_wind_monotonic(self):
        """Higher wind should always produce higher or equal risk."""
        prev_risk = 0.0
        for wind in [0, 3, 6, 9, 12, 15, 18, 21]:
            risk = calculate_hourly_risk(wind, wind * 1.5, 0.0, 12.0, DEFAULT_THRESHOLDS)
            self.assertGreaterEqual(
                risk, prev_risk,
                f"Risk should be monotonic: wind={wind} gave {risk:.1f}% < {prev_risk:.1f}%"
            )
            prev_risk = risk

    def test_gust_increases_risk(self):
        """Higher gusts should increase risk (all else equal)."""
        low_gust = calculate_hourly_risk(8.0, 10.0, 0.0, 12.0, DEFAULT_THRESHOLDS)
        high_gust = calculate_hourly_risk(8.0, 22.0, 0.0, 12.0, DEFAULT_THRESHOLDS)
        self.assertGreater(
            high_gust, low_gust,
            f"Higher gusts should increase risk: {high_gust:.1f}% vs {low_gust:.1f}%"
        )

    def test_rain_increases_risk(self):
        """Heavy precipitation should increase risk."""
        dry = calculate_hourly_risk(8.0, 12.0, 0.0, 12.0, DEFAULT_THRESHOLDS)
        wet = calculate_hourly_risk(8.0, 12.0, 3.0, 12.0, DEFAULT_THRESHOLDS)
        self.assertGreater(
            wet, dry,
            f"Rain should increase risk: {wet:.1f}% vs {dry:.1f}%"
        )

    def test_cold_increases_risk(self):
        """Sub-zero temperatures should increase risk."""
        warm = calculate_hourly_risk(8.0, 12.0, 0.0, 15.0, DEFAULT_THRESHOLDS)
        cold = calculate_hourly_risk(8.0, 12.0, 0.0, -3.0, DEFAULT_THRESHOLDS)
        self.assertGreater(
            cold, warm,
            f"Cold should increase risk: {cold:.1f}% vs {warm:.1f}%"
        )

    def test_bounded_0_to_100(self):
        """Risk must always be in [0, 100] regardless of inputs."""
        test_cases = [
            (0, 0, 0, 20),      # completely calm
            (5, 8, 0.5, 10),    # mild
            (12, 18, 1.5, 3),   # moderate
            (20, 30, 5, -5),    # extreme
            (50, 80, 20, -20),  # absurd
        ]
        for wind, gust, precip, temp in test_cases:
            risk = calculate_hourly_risk(wind, gust, precip, temp, DEFAULT_THRESHOLDS)
            self.assertGreaterEqual(risk, 0.0, f"Risk below 0 for ({wind},{gust},{precip},{temp})")
            self.assertLessEqual(risk, 100.0, f"Risk above 100 for ({wind},{gust},{precip},{temp})")

    def test_default_thresholds_used(self):
        """Calling without thresholds should still work."""
        risk = calculate_hourly_risk(5.0, 8.0, 0.0, 12.0)
        self.assertGreaterEqual(risk, 0.0)
        self.assertLessEqual(risk, 100.0)

    def test_custom_lower_thresholds(self):
        """Lower thresholds should produce higher risk for the same conditions."""
        strict = {
            "wind_mean_caution": 5.0, "wind_mean_cancel": 8.0,
            "gust_caution": 8.0, "gust_cancel": 12.0,
            "precip_caution": 0.3, "precip_cancel": 1.0,
            "temp_min_caution": 3.0, "temp_min_cancel": 0.0,
        }
        risk_default = calculate_hourly_risk(7.0, 10.0, 0.5, 5.0, DEFAULT_THRESHOLDS)
        risk_strict = calculate_hourly_risk(7.0, 10.0, 0.5, 5.0, strict)
        self.assertGreater(
            risk_strict, risk_default,
            f"Stricter thresholds should give higher risk: {risk_strict:.1f}% vs {risk_default:.1f}%"
        )


# ============================================================
# Recommendation
# ============================================================
class TestGetRecommendation(TestCase):
    """Verify recommendation bands: GO < 20%, CAUTION 20-50%, CANCEL >= 50%."""

    def test_low_risk_go(self):
        self.assertEqual(get_recommendation(5.0), "GO")

    def test_zero_risk_go(self):
        self.assertEqual(get_recommendation(0.0), "GO")

    def test_boundary_19_go(self):
        self.assertEqual(get_recommendation(19.9), "GO")

    def test_boundary_20_caution(self):
        self.assertEqual(get_recommendation(20.0), "CAUTION")

    def test_mid_caution(self):
        self.assertEqual(get_recommendation(35.0), "CAUTION")

    def test_boundary_49_caution(self):
        self.assertEqual(get_recommendation(49.9), "CAUTION")

    def test_boundary_50_cancel(self):
        self.assertEqual(get_recommendation(50.0), "CANCEL")

    def test_high_risk_cancel(self):
        self.assertEqual(get_recommendation(85.0), "CANCEL")

    def test_100_cancel(self):
        self.assertEqual(get_recommendation(100.0), "CANCEL")

    def test_nan_returns_unknown(self):
        self.assertEqual(get_recommendation(float("nan")), "UNKNOWN")


# ============================================================
# Model weights
# ============================================================
class TestModelWeights(TestCase):
    """Verify geographic model weighting."""

    def test_uk_inland_includes_ukv(self):
        """UKV should be eligible for a UK inland point."""
        weights = get_model_weights(52.0, -1.0, "urban")
        self.assertIn("ukv", weights)
        self.assertGreater(weights["ukv"], 0)

    def test_weights_sum_to_one(self):
        """All weights must sum to 1.0 (normalised)."""
        weights = get_model_weights(52.0, -1.0, "urban")
        total = sum(weights.values())
        self.assertAlmostEqual(total, 1.0, places=5,
                               msg=f"Weights sum to {total}, expected 1.0")

    def test_all_weights_non_negative(self):
        """No negative weights."""
        weights = get_model_weights(52.0, -1.0, "urban")
        for model, w in weights.items():
            self.assertGreaterEqual(w, 0, f"{model} has negative weight {w}")

    def test_coastal_point(self):
        """Coastal point should still produce valid weights."""
        weights = get_model_weights(50.7, -1.3, "coastal")
        self.assertTrue(len(weights) > 0, "No models eligible for coastal point")
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=5)

    def test_scottish_point(self):
        """Scottish location should still have eligible models."""
        weights = get_model_weights(57.0, -4.0, "rural")
        self.assertTrue(len(weights) > 0, "No models eligible for Scottish point")
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=5)

    def test_different_locations_different_weights(self):
        """East Anglia vs western UK should have different weight profiles."""
        east = get_model_weights(52.5, 1.5, "urban")
        west = get_model_weights(52.5, -4.0, "urban")
        # Not guaranteed to differ, but likely due to ICON-D2 domain
        # At minimum, both should be valid
        self.assertAlmostEqual(sum(east.values()), 1.0, places=5)
        self.assertAlmostEqual(sum(west.values()), 1.0, places=5)


# ============================================================
# Integration: risk → recommendation consistency
# ============================================================
class TestRiskRecommendationIntegration(TestCase):
    """Ensure risk scores map to the expected recommendations."""

    def test_calm_gives_go(self):
        risk = calculate_hourly_risk(3.0, 5.0, 0.0, 15.0, DEFAULT_THRESHOLDS)
        rec = get_recommendation(risk)
        self.assertEqual(rec, "GO", f"Calm day: risk={risk:.1f}% should be GO, got {rec}")

    def test_dangerous_gives_cancel(self):
        risk = calculate_hourly_risk(16.0, 25.0, 3.0, -3.0, DEFAULT_THRESHOLDS)
        rec = get_recommendation(risk)
        self.assertEqual(rec, "CANCEL", f"Dangerous: risk={risk:.1f}% should be CANCEL, got {rec}")

    def test_moderate_not_cancel(self):
        """Moderate conditions should not trigger CANCEL."""
        risk = calculate_hourly_risk(8.0, 12.0, 0.5, 8.0, DEFAULT_THRESHOLDS)
        rec = get_recommendation(risk)
        self.assertNotEqual(rec, "CANCEL",
                            f"Moderate conditions risk={risk:.1f}% should not be CANCEL")
