"""
The run_forecasts management command: output for failed runs, the site lock
on --site, and a non-zero exit when any site failed so the cron shows it.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from forecasts import locking
from forecasts.models import ForecastRun
from sites.models import Client, Site


@patch("sites.signals.queue_forecast_generation", return_value=True)
class RunForecastsCommandTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="Acme Rope")
        self.site = Site.objects.create(
            client=client, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )

    def _run(self, status, **kwargs):
        defaults = dict(
            site=self.site, forecast_date=timezone.localdate(), status=status,
            models_used=[],
        )
        defaults.update(kwargs)
        return ForecastRun.objects.create(**defaults)

    def test_single_site_failure_is_reported_not_a_crash(self, _q):
        """A failed run has no peak_risk; formatting it raised TypeError."""
        failed = self._run(ForecastRun.Status.FAILED, error_message="All models failed")
        out = StringIO()

        with patch(
            "forecasts.management.commands.run_forecasts.run_forecast_for_site",
            return_value=[failed],
        ):
            with self.assertRaises(CommandError):
                call_command("run_forecasts", site=self.site.pk, stdout=out)

        self.assertIn("All models failed", out.getvalue())

    def test_single_site_success_prints_the_verdict(self, _q):
        ok = self._run(ForecastRun.Status.SUCCESS, peak_risk=12.3, recommendation="GO")
        out = StringIO()

        with patch(
            "forecasts.management.commands.run_forecasts.run_forecast_for_site",
            return_value=[ok],
        ):
            call_command("run_forecasts", site=self.site.pk, stdout=out)

        self.assertIn("GO (peak risk 12.3%)", out.getvalue())

    def test_single_site_respects_the_lock(self, _q):
        locking.acquire(self.site.pk)

        with patch(
            "forecasts.management.commands.run_forecasts.run_forecast_for_site",
        ) as run:
            with self.assertRaises(CommandError):
                call_command("run_forecasts", site=self.site.pk, stdout=StringIO())

        run.assert_not_called()

    def test_single_site_releases_the_lock(self, _q):
        with patch(
            "forecasts.management.commands.run_forecasts.run_forecast_for_site",
            return_value=[],
        ):
            call_command("run_forecasts", site=self.site.pk, stdout=StringIO())

        self.assertTrue(locking.acquire(self.site.pk))

    def test_unknown_site_is_an_error(self, _q):
        with self.assertRaises(CommandError):
            call_command("run_forecasts", site=999999, stdout=StringIO())

    def test_all_sites_exits_non_zero_on_a_failed_run(self, _q):
        failed = self._run(ForecastRun.Status.FAILED)

        with patch(
            "forecasts.management.commands.run_forecasts.run_forecasts_all_active",
            return_value=([failed], []),
        ):
            with self.assertRaises(CommandError):
                call_command("run_forecasts", stdout=StringIO())

    def test_all_sites_exits_non_zero_when_a_site_raised(self, _q):
        """A site that raised leaves no run row, so it must be counted separately."""
        with patch("forecasts.engine.runner.run_forecast_for_site", side_effect=RuntimeError("boom")):
            with self.assertRaises(CommandError) as ctx:
                call_command("run_forecasts", stdout=StringIO())

        self.assertIn("Tower", str(ctx.exception))

    def test_all_sites_succeeds_quietly_when_everything_worked(self, _q):
        ok = self._run(ForecastRun.Status.SUCCESS, peak_risk=5.0, recommendation="GO")
        out = StringIO()

        with patch(
            "forecasts.management.commands.run_forecasts.run_forecasts_all_active",
            return_value=([ok], []),
        ):
            call_command("run_forecasts", stdout=out)

        self.assertIn("1 successful, 0 failed", out.getvalue())
