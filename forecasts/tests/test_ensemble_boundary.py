"""
A member exactly on a limit breaches it, in the ensemble probabilities too.

core.evaluate_thresholds, the map's marker gate and the risk grid all gate
with >=. The per-site ensemble counted members with >, so a gust exactly on
the cancel limit cancelled the day's verdict but was not counted towards
the chance of cancellation printed beside it.
"""

from datetime import date

from django.test import SimpleTestCase

from forecasts.engine import ensemble
from forecasts.engine.core import evaluate_thresholds
from forecasts.tests.test_ensemble import TH, make_members, make_times


class EnsembleBoundaryTests(SimpleTestCase):
    def _day(self, members):
        out = ensemble.cancellation_probability(make_times(), members, TH)
        return out[date(2026, 1, 15)]

    def test_gust_exactly_on_cancel_counts_as_cancelling(self):
        self.assertEqual(self._day(make_members([20.0]))["p_cancel"], 1.0)

    def test_gust_exactly_on_caution_counts_as_caution(self):
        day = self._day(make_members([15.0]))
        self.assertEqual(day["p_caution"], 1.0)
        self.assertEqual(day["p_cancel"], 0.0)

    def test_wind_and_rain_exactly_on_cancel_count(self):
        self.assertEqual(self._day(make_members([5.0], wind=14.0))["p_cancel"], 1.0)
        self.assertEqual(self._day(make_members([5.0], precip=2.0))["p_cancel"], 1.0)

    def test_just_below_the_limit_does_not(self):
        self.assertEqual(self._day(make_members([19.999]))["p_cancel"], 0.0)

    def test_agrees_with_the_deterministic_verdict_on_the_limit(self):
        verdict, _ = evaluate_thresholds(
            wind=2.0, gust=20.0, precip=0.0, temp=10.0, thresholds=TH,
        )
        self.assertEqual(verdict, "CANCEL")
        self.assertEqual(self._day(make_members([20.0]))["p_cancel"], 1.0)

    def test_count_breaches_on_the_limit(self):
        counts = ensemble.count_breaches(make_members([20.0, 19.0]), TH, 24)
        self.assertEqual(counts, [1] * 24)
