"""
Tests for cleanup_forecasts, including the UK grid sweep.

risk_grid prunes its own old runs, but only after a run succeeds — so a
failing grid job stopped pruning at exactly the moment data was still piling
up. Grid points and the cached contour PNGs are the largest thing in this
database, so retention needs to keep working from a second cron.
"""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from forecasts.models import (
    CachedContourImage,
    ForecastRun,
    UKRiskGridPoint,
    UKRiskGridRun,
)
from sites.models import Client, Site


def make_grid_run(days_ago):
    run = UKRiskGridRun.objects.create(
        forecast_date=timezone.localdate() - timedelta(days=days_ago),
        status=UKRiskGridRun.Status.SUCCESS,
        lat_min=49.9, lat_max=58.7, lon_min=-7.6, lon_max=1.8,
    )
    ts = timezone.now() - timedelta(days=days_ago)
    UKRiskGridPoint.objects.create(
        run=run, latitude=55.0, longitude=-3.0, timestamp=ts,
        wind_speed=5.0, wind_gusts=9.0, precipitation=0.0,
        temperature=12.0, risk=10.0,
    )
    CachedContourImage.objects.create(
        run=run, timestamp=ts, variable="risk", image_data=b"\x89PNG-fake",
    )
    return run


class GridCleanupTests(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("cleanup_forecasts", *args, stdout=out)
        return out.getvalue()

    def test_old_grid_runs_are_deleted(self):
        make_grid_run(days_ago=10)
        self._run("--grid-days", "2")

        self.assertEqual(UKRiskGridRun.objects.count(), 0)

    def test_recent_grid_runs_are_kept(self):
        make_grid_run(days_ago=0)
        self._run("--grid-days", "2")

        self.assertEqual(UKRiskGridRun.objects.count(), 1)

    def test_deleting_a_run_takes_its_points_and_images(self):
        """These are the rows that actually consume the disk."""
        make_grid_run(days_ago=10)
        self._run("--grid-days", "2")

        self.assertEqual(UKRiskGridPoint.objects.count(), 0)
        self.assertEqual(CachedContourImage.objects.count(), 0)

    def test_dry_run_reports_without_deleting(self):
        make_grid_run(days_ago=10)
        output = self._run("--grid-days", "2", "--dry-run")

        self.assertIn("Would delete 1 UK grid runs", output)
        self.assertEqual(UKRiskGridRun.objects.count(), 1)

    def test_default_grid_window_matches_risk_grid_retention(self):
        make_grid_run(days_ago=3)
        self._run()

        self.assertEqual(UKRiskGridRun.objects.count(), 0)


class ForecastRunCleanupTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="Acme Rope")
        self.site = Site.objects.create(
            client=client, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )

    def _make_run(self, days_ago):
        run = ForecastRun.objects.create(
            site=self.site, forecast_date=timezone.localdate(),
            status=ForecastRun.Status.SUCCESS,
        )
        # generated_at is auto_now_add, so move it afterwards.
        ForecastRun.objects.filter(pk=run.pk).update(
            generated_at=timezone.now() - timedelta(days=days_ago)
        )
        return run

    def test_old_runs_are_deleted(self):
        self._make_run(days_ago=60)
        call_command("cleanup_forecasts", "--days", "30", stdout=StringIO())

        self.assertEqual(ForecastRun.objects.count(), 0)

    def test_recent_runs_are_kept(self):
        self._make_run(days_ago=1)
        call_command("cleanup_forecasts", "--days", "30", stdout=StringIO())

        self.assertEqual(ForecastRun.objects.count(), 1)

    def test_forecast_and_grid_windows_are_independent(self):
        """A 30-day forecast window must not drag grid data along with it."""
        self._make_run(days_ago=1)
        make_grid_run(days_ago=10)

        call_command(
            "cleanup_forecasts", "--days", "30", "--grid-days", "2",
            stdout=StringIO(),
        )

        self.assertEqual(ForecastRun.objects.count(), 1)
        self.assertEqual(UKRiskGridRun.objects.count(), 0)
