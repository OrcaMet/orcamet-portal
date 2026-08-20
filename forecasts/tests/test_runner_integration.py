"""
End-to-end runner behaviour after the redesign.

Covers the two things that changed shape: the verdict now comes from a hard
gate rather than the weighted score, and ensemble probabilities ride
alongside without being able to break the run.
"""

from datetime import date, datetime, timezone
from unittest.mock import patch

import pandas as pd
from django.test import TestCase

from forecasts.engine import ensemble as ens
from forecasts.engine import runner
from forecasts.models import ForecastRun, HourlyForecast
from sites.models import Client, Site, ThresholdProfile

DAY = date(2026, 1, 15)          # GMT, so local hours equal UTC hours


def frame(gust=4.0, wind=2.0, precip=0.0, temp=10.0):
    times = pd.to_datetime(
        [datetime(2026, 1, 15, h, tzinfo=timezone.utc) for h in range(24)],
        utc=True,
    )
    df = pd.DataFrame({
        "time": times,
        "wind_speed": [wind] * 24,
        "wind_gusts": [gust] * 24,
        "precipitation": [precip] * 24,
        "temperature": [temp] * 24,
        "wind_speed_spread": [0.0] * 24,
        "wind_gusts_spread": [0.0] * 24,
        "precipitation_spread": [0.0] * 24,
        "temperature_spread": [0.0] * 24,
        "n_models": [4] * 24,
    })
    df.attrs["models_used"] = ["ukv", "ecmwf"]
    return df


def ensemble_payload(gusts):
    times = [f"2026-01-15T{h:02d}:00" for h in range(24)]
    members = [
        {
            "wind_speed_10m": [2.0] * 24,
            "wind_gusts_10m": [g] * 24,
            "precipitation": [0.0] * 24,
            "temperature_2m": [10.0] * 24,
        }
        for g in gusts
    ]
    return times, members


@patch("sites.signals.queue_forecast_generation", return_value=True)
class RunnerVerdictTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="Acme Rope")
        self.site = Site.objects.create(
            client=client, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )
        ThresholdProfile.objects.create(site=self.site)

    def _run(self, df, members=None):
        fetch = (
            patch.object(ens, "fetch_members", return_value=members)
            if members is not None
            else patch.object(ens, "fetch_members", side_effect=ens.EnsembleUnavailable("none"))
        )
        with patch.object(runner, "fetch_ensemble", return_value=df), \
                patch.object(runner.dj_timezone, "localdate", return_value=DAY), \
                fetch:
            return runner.run_forecast_for_site(self.site)

    def test_gust_breach_cancels_even_though_the_old_score_would_not(self, _q):
        """Gusts alone scored 42.6% under the weighted model — CAUTION."""
        self._run(frame(gust=30.0))

        run = ForecastRun.objects.get(forecast_date=DAY)
        self.assertEqual(run.recommendation, "CANCEL")
        self.assertEqual(run.limiting_variable, "gust")
        self.assertLess(run.peak_risk, 50.0)

    def test_rain_alone_cancels(self, _q):
        self._run(frame(precip=10.0))

        run = ForecastRun.objects.get(forecast_date=DAY)
        self.assertEqual(run.recommendation, "CANCEL")
        self.assertEqual(run.limiting_variable, "precip")

    def test_calm_day_is_go(self, _q):
        self._run(frame())
        self.assertEqual(ForecastRun.objects.get(forecast_date=DAY).recommendation, "GO")

    def test_max_temp_is_recorded(self, _q):
        """Without it, a heat-driven verdict has nothing to point at."""
        self._run(frame(temp=35.0))

        run = ForecastRun.objects.get(forecast_date=DAY)
        self.assertEqual(run.max_temp, 35.0)
        self.assertEqual(run.limiting_variable, "temperature")


@patch("sites.signals.queue_forecast_generation", return_value=True)
class RunnerProbabilityTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="Acme Rope")
        self.site = Site.objects.create(
            client=client, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )
        ThresholdProfile.objects.create(site=self.site)

    def _run(self, df, members=None):
        fetch = (
            patch.object(ens, "fetch_members", return_value=members)
            if members is not None
            else patch.object(ens, "fetch_members", side_effect=ens.EnsembleUnavailable("none"))
        )
        with patch.object(runner, "fetch_ensemble", return_value=df), \
                patch.object(runner.dj_timezone, "localdate", return_value=DAY), \
                fetch:
            return runner.run_forecast_for_site(self.site)

    def test_probability_is_stored(self, _q):
        # 2 of 4 members over the 20 m/s gust cancel limit.
        self._run(frame(), ensemble_payload([25.0, 25.0, 5.0, 5.0]))

        run = ForecastRun.objects.get(forecast_date=DAY)
        self.assertAlmostEqual(run.p_cancel, 0.5)
        self.assertEqual(run.ensemble_members, 4)
        self.assertAlmostEqual(run.p_cancel_by_variable["gust_cancel"], 0.5)

    def test_percentiles_land_on_the_hourly_rows(self, _q):
        self._run(frame(), ensemble_payload([5.0, 10.0, 15.0, 20.0]))

        hour = HourlyForecast.objects.filter(run__forecast_date=DAY).first()
        self.assertIn("wind_gusts_10m", hour.percentiles)
        self.assertIn("p90", hour.percentiles["wind_gusts_10m"])

    def test_verdict_and_probability_are_independent(self, _q):
        """
        A calm central forecast with scattered members: GO, but with a real
        chance of cancellation. This is the case the supervisor needs to see.
        """
        self._run(frame(gust=4.0), ensemble_payload([25.0] + [4.0] * 9))

        run = ForecastRun.objects.get(forecast_date=DAY)
        self.assertEqual(run.recommendation, "GO")
        self.assertAlmostEqual(run.p_cancel, 0.1)

    def test_ensemble_outage_does_not_fail_the_run(self, _q):
        """The verdict must not depend on the ensemble being reachable."""
        runs = self._run(frame(gust=30.0))

        self.assertTrue(runs)
        run = ForecastRun.objects.get(forecast_date=DAY)
        self.assertEqual(run.status, ForecastRun.Status.SUCCESS)
        self.assertEqual(run.recommendation, "CANCEL")

    def test_missing_probability_is_null_not_zero(self, _q):
        """A zero would read as 'certainly fine' rather than 'unknown'."""
        self._run(frame())

        run = ForecastRun.objects.get(forecast_date=DAY)
        self.assertIsNone(run.p_cancel)
        self.assertIsNone(run.ensemble_members)
        self.assertEqual(run.p_cancel_by_variable, {})
