"""
Which ForecastRun the dashboard and map headline for a site.

One forecast pass writes today, tomorrow and the day after, in that order.
Picking the newest row by generated_at therefore always picked the furthest
day, and the home page showed the day after tomorrow's verdict as the
site's "Latest Risk".
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from dashboard.views import _latest_runs_by_site
from forecasts.models import ForecastRun
from sites.models import Client, Site


@patch("sites.signals.queue_forecast_generation", return_value=True)
class HeadlineRunTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="Acme Rope")
        self.site = Site.objects.create(
            client=client, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )
        self.user = User.objects.create_user(
            username="dave", role=User.Role.CLIENT_ADMIN, client=client,
        )
        self.today = timezone.localdate()
        self.now = timezone.now()

    def _run(self, day_offset, recommendation, generated_offset_s=0, **kwargs):
        defaults = dict(
            site=self.site,
            forecast_date=self.today + timedelta(days=day_offset),
            generated_at=self.now + timedelta(seconds=generated_offset_s),
            status=ForecastRun.Status.SUCCESS,
            peak_risk=10.0, recommendation=recommendation,
            models_used=["ukv"],
        )
        defaults.update(kwargs)
        return ForecastRun.objects.create(**defaults)

    def _one_pass(self):
        """Today, tomorrow, day after — written in that order, as the runner does."""
        return [
            self._run(0, "CANCEL", generated_offset_s=0),
            self._run(1, "GO", generated_offset_s=1),
            self._run(2, "GO", generated_offset_s=2),
        ]

    def test_today_is_headlined_not_the_last_day_written(self, _q):
        today_run, _, _ = self._one_pass()

        latest = _latest_runs_by_site([self.site])

        self.assertEqual(latest[self.site.id], today_run)

    def test_home_page_shows_todays_verdict(self, _q):
        self._one_pass()
        self.client.force_login(self.user)

        r = self.client.get("/dashboard/")

        self.assertContains(r, "CANCEL")
        self.assertEqual(r.context["alert_count"], 1)

    def test_map_pin_carries_todays_date(self, _q):
        self._one_pass()
        self.client.force_login(self.user)

        props = self.client.get("/dashboard/map/sites.json").json()["features"][0]["properties"]

        self.assertEqual(props["forecast_date"], self.today.isoformat())
        self.assertEqual(props["recommendation"], "CANCEL")

    def test_nearest_upcoming_day_when_today_is_missing(self, _q):
        tomorrow = self._run(1, "CAUTION", generated_offset_s=0)
        self._run(2, "GO", generated_offset_s=1)

        self.assertEqual(_latest_runs_by_site([self.site])[self.site.id], tomorrow)

    def test_newest_attempt_at_the_day_wins(self, _q):
        self._run(0, "GO", generated_offset_s=0)
        failed = self._run(
            0, "", generated_offset_s=5,
            status=ForecastRun.Status.FAILED, peak_risk=None,
        )

        self.assertEqual(
            _latest_runs_by_site([self.site], success_only=False)[self.site.id],
            failed,
        )

    def test_falls_back_to_the_most_recent_past_day(self, _q):
        self._run(-3, "GO")
        yesterday = self._run(-1, "CAUTION")

        self.assertEqual(_latest_runs_by_site([self.site])[self.site.id], yesterday)
