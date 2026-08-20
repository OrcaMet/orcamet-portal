"""
The verdict is a hard gate, not a weighted score.

The weighted form could not express a breach: gusts carry 0.40 on a scale
centred at 0.45, so gusts alone topped out at 42.6% (CAUTION) at any speed,
and rain or temperature alone never left GO however extreme.
"""

from django.test import TestCase

from forecasts.engine.core import calculate_hourly_risk, evaluate_thresholds

TH = {
    "wind_mean_caution": 10.0, "wind_mean_cancel": 14.0,
    "gust_caution": 15.0, "gust_cancel": 20.0,
    "precip_caution": 0.7, "precip_cancel": 2.0,
    "temp_min_caution": 1.0, "temp_min_cancel": -2.0,
    "temp_max_caution": 27.0, "temp_max_cancel": 32.0,
}

CALM = dict(wind=2.0, gust=4.0, precip=0.0, temp=15.0)


def verdict(**overrides):
    args = dict(CALM)
    args.update(overrides)
    return evaluate_thresholds(
        args["wind"], args["gust"], args["precip"], args["temp"], TH
    )


class SingleVariableCancelsTests(TestCase):
    """Each of these returned CAUTION or GO under the old scoring."""

    def test_gusts_at_the_cancel_limit_cancel(self):
        self.assertEqual(verdict(gust=20.0), ("CANCEL", "gust"))

    def test_extreme_gusts_cancel(self):
        self.assertEqual(verdict(gust=100.0), ("CANCEL", "gust"))

    def test_wind_at_the_cancel_limit_cancels(self):
        self.assertEqual(verdict(wind=14.0), ("CANCEL", "wind"))

    def test_precipitation_at_the_cancel_limit_cancels(self):
        self.assertEqual(verdict(precip=2.0), ("CANCEL", "precip"))

    def test_cold_at_the_cancel_limit_cancels(self):
        self.assertEqual(verdict(temp=-2.0), ("CANCEL", "temperature"))

    def test_heat_at_the_cancel_limit_cancels(self):
        self.assertEqual(verdict(temp=32.0), ("CANCEL", "temperature"))

    def test_old_scoring_would_not_have_cancelled_any_of_these(self):
        """Pins the behaviour this replaces, so the contrast stays visible."""
        for label, kwargs in [
            ("gusts 100", dict(gust=100.0)),
            ("precip 50", dict(precip=50.0)),
            ("temp 45", dict(temp=45.0)),
        ]:
            args = dict(CALM)
            args.update(kwargs)
            old = calculate_hourly_risk(
                args["wind"], args["gust"], args["precip"], args["temp"], TH
            )
            with self.subTest(case=label):
                self.assertLess(old, 50.0)
                self.assertEqual(verdict(**kwargs)[0], "CANCEL")


class CautionBandTests(TestCase):
    def test_calm_is_go(self):
        self.assertEqual(verdict(), ("GO", None))

    def test_at_the_caution_limit_is_caution(self):
        """The old ramp was still zero here, scoring identical to dead calm."""
        self.assertEqual(verdict(gust=15.0), ("CAUTION", "gust"))

    def test_just_below_caution_is_go(self):
        self.assertEqual(verdict(gust=14.9), ("GO", None))

    def test_cold_caution(self):
        self.assertEqual(verdict(temp=1.0), ("CAUTION", "temperature"))

    def test_heat_caution(self):
        self.assertEqual(verdict(temp=27.0), ("CAUTION", "temperature"))

    def test_mild_temperature_is_go(self):
        self.assertEqual(verdict(temp=15.0), ("GO", None))


class WorstVariableWinsTests(TestCase):
    def test_cancel_outranks_caution(self):
        v, limiting = verdict(gust=16.0, wind=14.0)
        self.assertEqual(v, "CANCEL")
        self.assertEqual(limiting, "wind")

    def test_a_single_cancel_among_calm_variables_still_cancels(self):
        self.assertEqual(verdict(precip=5.0)[0], "CANCEL")

    def test_limiting_variable_is_reported(self):
        self.assertEqual(verdict(precip=5.0)[1], "precip")

    def test_gust_named_when_gust_and_wind_both_cancel(self):
        self.assertEqual(verdict(gust=25.0, wind=20.0)[1], "gust")


class MissingDataTests(TestCase):
    def test_missing_values_are_skipped_not_treated_as_calm(self):
        v, limiting = evaluate_thresholds(None, 25.0, None, None, TH)
        self.assertEqual((v, limiting), ("CANCEL", "gust"))

    def test_nan_is_skipped(self):
        v, _ = evaluate_thresholds(float("nan"), 4.0, 0.0, 15.0, TH)
        self.assertEqual(v, "GO")

    def test_absent_heat_threshold_disables_the_heat_gate(self):
        th = dict(TH, temp_max_caution=None, temp_max_cancel=None)
        v, _ = evaluate_thresholds(2.0, 4.0, 0.0, 40.0, th)
        self.assertEqual(v, "GO")
