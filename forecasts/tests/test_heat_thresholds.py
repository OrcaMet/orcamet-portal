"""
Tests for the heat (too-hot) end of the temperature threshold.

Temperature is scored two-sided: cold at one end, heat at the other, sharing
a single weight. The cases that matter are that heat actually registers, that
it does not double-count with cold, and that a thresholds dict without heat
keys behaves exactly as it did before heat existed.
"""

import numpy as np
from django.core.exceptions import ValidationError
from django.test import TestCase

from forecasts.engine.core import calculate_hourly_risk, temperature_ramp
from forecasts.models import MapThresholds
from sites.models import Client, Site, ThresholdProfile


COLD_ONLY = {
    "wind_mean_caution": 10.0, "wind_mean_cancel": 14.0,
    "gust_caution": 15.0, "gust_cancel": 20.0,
    "precip_caution": 0.7, "precip_cancel": 2.0,
    "temp_min_caution": 1.0, "temp_min_cancel": -2.0,
}

WITH_HEAT = {**COLD_ONLY, "temp_max_caution": 27.0, "temp_max_cancel": 32.0}


class TemperatureRampTests(TestCase):
    def test_comfortable_temperature_scores_zero_at_both_ends(self):
        self.assertEqual(temperature_ramp(15.0, WITH_HEAT), 0.0)

    def test_heat_ramps_between_caution_and_cancel(self):
        self.assertEqual(temperature_ramp(27.0, WITH_HEAT), 0.0)
        self.assertAlmostEqual(temperature_ramp(29.5, WITH_HEAT), 0.5)
        self.assertEqual(temperature_ramp(32.0, WITH_HEAT), 1.0)

    def test_above_heat_cancel_is_capped_at_one(self):
        self.assertEqual(temperature_ramp(45.0, WITH_HEAT), 1.0)

    def test_cold_end_still_works_with_heat_configured(self):
        self.assertEqual(temperature_ramp(1.0, WITH_HEAT), 0.0)
        self.assertAlmostEqual(temperature_ramp(-0.5, WITH_HEAT), 0.5)
        self.assertEqual(temperature_ramp(-2.0, WITH_HEAT), 1.0)

    def test_missing_heat_keys_score_cold_only(self):
        """Historic snapshots and pre-heat callers must be unaffected."""
        self.assertEqual(temperature_ramp(35.0, COLD_ONLY), 0.0)

    def test_explicit_none_disables_heat(self):
        off = {**COLD_ONLY, "temp_max_caution": None, "temp_max_cancel": None}
        self.assertEqual(temperature_ramp(35.0, off), 0.0)

    def test_a_lone_heat_value_is_ignored_rather_than_crashing(self):
        half = {**COLD_ONLY, "temp_max_caution": 27.0}
        self.assertEqual(temperature_ramp(35.0, half), 0.0)

    def test_nan_temperature_propagates(self):
        """max() would otherwise pick a real number over NaN, hiding a gap."""
        self.assertTrue(np.isnan(temperature_ramp(float("nan"), WITH_HEAT)))
        self.assertTrue(np.isnan(temperature_ramp(float("nan"), COLD_ONLY)))


class HeatInRiskScoreTests(TestCase):
    def _calm_day(self, temp, thresholds):
        """Benign wind/rain, so only the temperature term moves the score."""
        return calculate_hourly_risk(2.0, 4.0, 0.0, temp, thresholds)

    def test_hot_day_scores_higher_than_a_mild_one(self):
        mild = self._calm_day(18.0, WITH_HEAT)
        hot = self._calm_day(34.0, WITH_HEAT)
        self.assertGreater(hot, mild)

    def test_hot_day_is_unchanged_when_heat_is_not_configured(self):
        self.assertEqual(
            self._calm_day(34.0, COLD_ONLY), self._calm_day(18.0, COLD_ONLY)
        )

    def test_temperature_does_not_count_twice(self):
        """
        Cold and heat share one weight, so the worst a temperature-only day
        can score is the same whichever end drives it.
        """
        freezing = self._calm_day(-10.0, WITH_HEAT)
        scorching = self._calm_day(40.0, WITH_HEAT)
        self.assertAlmostEqual(freezing, scorching)

    def test_default_thresholds_include_heat(self):
        """calculate_hourly_risk's own fallback must not be cold-only."""
        self.assertGreater(
            calculate_hourly_risk(2.0, 4.0, 0.0, 34.0, None),
            calculate_hourly_risk(2.0, 4.0, 0.0, 18.0, None),
        )


class MapThresholdsHeatTests(TestCase):
    def test_defaults_enable_heat(self):
        d = MapThresholds.load().as_dict()
        self.assertEqual(d["temp_max_caution"], 27.0)
        self.assertEqual(d["temp_max_cancel"], 32.0)

    def test_heat_cancel_below_caution_is_refused(self):
        obj = MapThresholds.load()
        obj.temp_max_caution = 32.0
        obj.temp_max_cancel = 27.0
        with self.assertRaises(ValidationError) as ctx:
            obj.full_clean()
        self.assertIn("temp_max_cancel", ctx.exception.error_dict)

    def test_half_filled_heat_pair_is_refused(self):
        """One value alone is silently ignored by the engine."""
        obj = MapThresholds.load()
        obj.temp_max_cancel = None
        with self.assertRaises(ValidationError) as ctx:
            obj.full_clean()
        self.assertIn("temp_max_cancel", ctx.exception.error_dict)

    def test_both_blank_is_allowed_and_disables_heat(self):
        obj = MapThresholds.load()
        obj.temp_max_caution = None
        obj.temp_max_cancel = None
        obj.full_clean()
        obj.save()

        self.assertIsNone(MapThresholds.load().as_dict()["temp_max_caution"])

    def test_heat_caution_below_cold_caution_is_refused(self):
        """Overlapping ends would make the two ramps fight."""
        obj = MapThresholds.load()
        obj.temp_min_caution = 5.0
        obj.temp_max_caution = 3.0
        obj.temp_max_cancel = 10.0
        with self.assertRaises(ValidationError) as ctx:
            obj.full_clean()
        self.assertIn("temp_max_caution", ctx.exception.error_dict)


class ThresholdProfileHeatTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="Acme Rope")
        self.site = Site.objects.create(
            client=client, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )

    def test_new_profiles_include_heat_by_default(self):
        profile = ThresholdProfile.objects.create(site=self.site)
        d = profile.as_dict()

        self.assertEqual(d["temp_max_caution"], 27.0)
        self.assertEqual(d["temp_max_cancel"], 32.0)

    def test_blanking_heat_disables_it_for_that_site(self):
        profile = ThresholdProfile.objects.create(
            site=self.site, temp_max_caution=None, temp_max_cancel=None,
        )
        profile.full_clean()

        self.assertIsNone(profile.as_dict()["temp_max_caution"])
        self.assertEqual(
            calculate_hourly_risk(2.0, 4.0, 0.0, 34.0, profile.as_dict()),
            calculate_hourly_risk(2.0, 4.0, 0.0, 18.0, profile.as_dict()),
        )

    def test_invalid_heat_ordering_is_refused(self):
        profile = ThresholdProfile(
            site=self.site, temp_max_caution=32.0, temp_max_cancel=27.0,
        )
        with self.assertRaises(ValidationError) as ctx:
            profile.full_clean()
        self.assertIn("temp_max_cancel", ctx.exception.error_dict)

    def test_existing_wind_validation_still_applies(self):
        profile = ThresholdProfile(
            site=self.site, gust_caution=20.0, gust_cancel=15.0,
        )
        with self.assertRaises(ValidationError) as ctx:
            profile.full_clean()
        self.assertIn("gust_cancel", ctx.exception.error_dict)
