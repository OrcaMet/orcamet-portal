"""
The work window is a local-time concept.

Applied to UTC hours, a 07:00-18:00 window actually scored 08:00-19:00 local
for the ~7 months of British Summer Time: the first hour of the real working
morning was never assessed, and an hour after knock-off was.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
from django.conf import settings
from django.test import TestCase
from django.utils import timezone as dj_timezone

from forecasts.engine import runner
from forecasts.models import ForecastRun, HourlyForecast
from sites.models import Client, Site, ThresholdProfile


class TimeZoneSettingTests(TestCase):
    def test_project_runs_on_uk_local_time(self):
        self.assertEqual(settings.TIME_ZONE, "Europe/London")

    def test_timestamps_are_still_stored_as_utc(self):
        """Local display must not mean local storage."""
        self.assertTrue(settings.USE_TZ)


def _summer_frame():
    """
    24 hours across a British Summer Time day, timestamped in UTC.

    On 1 July, local time is UTC+1 — so 06:00 UTC is 07:00 local, the first
    hour of the working day.
    """
    times = pd.to_datetime(
        [datetime(2026, 7, 1, h, tzinfo=timezone.utc) for h in range(24)],
        utc=True,
    )
    return pd.DataFrame({
        "time": times,
        "wind_speed": [5.0] * 24,
        "wind_gusts": [9.0] * 24,
        "precipitation": [0.0] * 24,
        "temperature": [15.0] * 24,
        "wind_speed_spread": [0.0] * 24,
        "wind_gusts_spread": [0.0] * 24,
        "precipitation_spread": [0.0] * 24,
        "temperature_spread": [0.0] * 24,
        "n_models": [4] * 24,
    })


@patch("sites.signals.queue_forecast_generation", return_value=True)
class WorkWindowIsLocalTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="Acme Rope")
        self.site = Site.objects.create(
            client=client, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )
        ThresholdProfile.objects.create(site=self.site)

    def _run(self):
        df = _summer_frame()
        df.attrs["models_used"] = ["ukv", "ecmwf"]
        with patch.object(runner, "fetch_ensemble", return_value=df), \
                patch.object(runner.dj_timezone, "localdate", return_value=date(2026, 7, 1)):
            return runner.run_forecast_for_site(self.site)

    def test_summary_covers_the_local_working_day(self, _queue):
        """
        07:00 local on 1 July is 06:00 UTC. The peak must be taken from the
        local window, so that hour has to be inside it.
        """
        runs = self._run()
        self.assertTrue(runs)

        run = ForecastRun.objects.get(forecast_date=date(2026, 7, 1))
        hours = list(
            HourlyForecast.objects.filter(run=run).order_by("timestamp")
        )
        self.assertTrue(hours, "no hourly rows stored")

        # Every stored hour keeps its UTC instant; only interpretation moved.
        local_hours = {
            dj_timezone.localtime(h.timestamp).hour for h in hours
        }
        self.assertIn(7, local_hours)

    def test_forecast_date_is_the_local_day(self, _queue):
        runs = self._run()
        self.assertEqual(runs[0].forecast_date, date(2026, 7, 1))

    def test_an_hour_only_inside_the_local_window_is_assessed(self, _queue):
        """
        06:00 UTC / 07:00 BST is in the local window but outside a naive UTC
        one. Spiking gusts there must move the day's peak.
        """
        df = _summer_frame()
        df.loc[df["time"].dt.hour == 6, "wind_gusts"] = 30.0
        df.attrs["models_used"] = ["ukv"]

        with patch.object(runner, "fetch_ensemble", return_value=df), \
                patch.object(runner.dj_timezone, "localdate", return_value=date(2026, 7, 1)):
            runner.run_forecast_for_site(self.site)

        run = ForecastRun.objects.get(forecast_date=date(2026, 7, 1))
        self.assertEqual(
            run.peak_gust, 30.0,
            "07:00 local was excluded from the work window — the UTC bug",
        )

    def test_an_hour_after_local_knock_off_is_excluded(self, _queue):
        """
        18:00 UTC is 19:00 BST — past the window. Under the old UTC logic it
        counted, dragging the day's peak up from an hour nobody was working.
        """
        df = _summer_frame()
        df.loc[df["time"].dt.hour == 18, "wind_gusts"] = 30.0
        df.attrs["models_used"] = ["ukv"]

        with patch.object(runner, "fetch_ensemble", return_value=df), \
                patch.object(runner.dj_timezone, "localdate", return_value=date(2026, 7, 1)):
            runner.run_forecast_for_site(self.site)

        run = ForecastRun.objects.get(forecast_date=date(2026, 7, 1))
        self.assertEqual(
            run.peak_gust, 9.0,
            "19:00 local was counted as a working hour",
        )

    def test_winter_has_no_offset_so_utc_and_local_agree(self, _queue):
        """GMT sanity check — the fix must not shift anything in winter."""
        times = pd.to_datetime(
            [datetime(2026, 1, 15, h, tzinfo=timezone.utc) for h in range(24)],
            utc=True,
        )
        df = _summer_frame()
        df["time"] = times
        df.loc[df["time"].dt.hour == 7, "wind_gusts"] = 30.0
        df.attrs["models_used"] = ["ukv"]

        with patch.object(runner, "fetch_ensemble", return_value=df), \
                patch.object(runner.dj_timezone, "localdate", return_value=date(2026, 1, 15)):
            runner.run_forecast_for_site(self.site)

        run = ForecastRun.objects.get(forecast_date=date(2026, 1, 15))
        self.assertEqual(run.peak_gust, 30.0)
