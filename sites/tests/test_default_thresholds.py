"""default_thresholds() is the one source of fallback limits."""

from django.test import SimpleTestCase

from forecasts.engine.core import calculate_hourly_risk
from sites.models import ThresholdProfile, default_thresholds


# The literal that used to be copied into the runner, the risk engine and the
# site detail view. Pinned here so moving to the model defaults changed no
# number anywhere.
PREVIOUS_LITERAL = {
    "wind_mean_caution": 10.0, "wind_mean_cancel": 14.0,
    "gust_caution": 15.0, "gust_cancel": 20.0,
    "precip_caution": 0.7, "precip_cancel": 2.0,
    "temp_min_caution": 1.0, "temp_min_cancel": -2.0,
    "temp_max_caution": 27.0, "temp_max_cancel": 32.0,
}


class DefaultThresholdTests(SimpleTestCase):
    def test_matches_the_previous_hardcoded_values(self):
        self.assertEqual(default_thresholds(), PREVIOUS_LITERAL)

    def test_matches_what_a_new_profile_gets(self):
        self.assertEqual(default_thresholds(), ThresholdProfile().as_dict())

    def test_is_a_fresh_dict_each_call(self):
        """A caller mutating its copy must not change anyone else's."""
        first = default_thresholds()
        first["gust_cancel"] = 99.0
        self.assertEqual(default_thresholds()["gust_cancel"], 20.0)

    def test_engine_fallback_uses_it(self):
        self.assertEqual(
            calculate_hourly_risk(12.0, 18.0, 1.0, 5.0),
            calculate_hourly_risk(12.0, 18.0, 1.0, 5.0, PREVIOUS_LITERAL),
        )
