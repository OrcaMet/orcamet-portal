"""
- The admin's "Latest Risk" column headlines today's run, not the furthest
  day out.
- Saving a site's active thresholds re-forecasts it, so stored verdicts are
  never scored against limits the site no longer has.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.test import TestCase
from django.utils import timezone

from forecasts.models import ForecastRun
from sites.admin import SiteAdmin
from sites.models import Client, Site, ThresholdProfile


def make_site(**kwargs):
    client = Client.objects.create(name=kwargs.pop("client_name", "Acme Rope"))
    defaults = dict(
        client=client, name="Tower", postcode="EH1 1YZ",
        latitude=55.95, longitude=-3.19,
    )
    defaults.update(kwargs)
    return Site.objects.create(**defaults)


@patch("sites.signals.queue_forecast_generation", return_value=True)
class AdminLatestRiskTests(TestCase):
    def test_shows_today_not_the_day_after_tomorrow(self, _q):
        site = make_site()
        today = timezone.localdate()
        now = timezone.now()
        for offset, rec in ((0, "CANCEL"), (1, "GO"), (2, "GO")):
            ForecastRun.objects.create(
                site=site, forecast_date=today + timedelta(days=offset),
                generated_at=now + timedelta(seconds=offset),
                status=ForecastRun.Status.SUCCESS,
                peak_risk=40.0 if rec == "CANCEL" else 5.0,
                recommendation=rec, models_used=[],
            )

        shown = SiteAdmin(Site, AdminSite()).latest_risk(site)

        self.assertIn("CANCEL", shown)

    def test_no_runs_shows_a_dash(self, _q):
        self.assertEqual(SiteAdmin(Site, AdminSite()).latest_risk(make_site()), "—")


@patch("sites.signals.queue_forecast_generation", return_value=True)
class ThresholdSaveReforecastTests(TestCase):
    def setUp(self):
        # Class-level @patch does not cover setUp; uncaptured on-commit
        # callbacks are discarded by TestCase, so nothing real runs here.
        self.site = make_site()
        self.profile = ThresholdProfile.objects.create(site=self.site)

    def _save(self, profile):
        with self.captureOnCommitCallbacks(execute=True):
            profile.save()

    def test_editing_active_thresholds_reforecasts(self, queue):
        self.profile.gust_cancel = 18.0
        self._save(self.profile)

        queue.assert_called_once_with(self.site.pk, self.site.name)

    def test_archiving_a_profile_does_not(self, queue):
        self.profile.is_active = False
        self._save(self.profile)

        queue.assert_not_called()

    def test_inactive_site_does_not(self, queue):
        Site.objects.filter(pk=self.site.pk).update(is_active=False)
        self.profile.refresh_from_db()
        self._save(self.profile)

        queue.assert_not_called()

    def test_completed_job_does_not(self, queue):
        Site.objects.filter(pk=self.site.pk).update(job_complete=True)
        self.profile.refresh_from_db()
        self._save(self.profile)

        queue.assert_not_called()
