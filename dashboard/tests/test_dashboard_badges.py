"""
The dashboard badge shows the verdict and the chance of cancellation.

It used to print peak_risk beside the verdict ("CANCEL — 27%"), which reads
as a 27% chance but is the weighted severity score. The site page and map
had already been changed to show the real chance of cancellation.
"""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from forecasts.models import ForecastRun
from sites.models import Client, Site


@patch("sites.signals.queue_forecast_generation", return_value=True)
class DashboardBadgeTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="Acme Rope")
        self.site = Site.objects.create(
            client=client, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )
        user = User.objects.create_user(
            username="dana", role=User.Role.CLIENT_ADMIN, client=client,
        )
        self.client.force_login(user)

    def _run(self, **kwargs):
        defaults = dict(
            site=self.site, forecast_date=timezone.localdate(),
            status=ForecastRun.Status.SUCCESS, recommendation="CANCEL",
            peak_risk=27.0, p_cancel=0.72, models_used=[],
        )
        defaults.update(kwargs)
        return ForecastRun.objects.create(**defaults)

    def test_shows_the_chance_of_cancellation(self, _q):
        self._run()
        r = self.client.get("/dashboard/")

        self.assertContains(r, "72% chance of cancellation")

    def test_does_not_print_the_severity_score_as_a_percentage(self, _q):
        self._run()
        r = self.client.get("/dashboard/")

        self.assertNotContains(r, "27%")
        self.assertNotContains(r, "CANCEL —")

    def test_unknown_probability_prints_no_figure(self, _q):
        """A missing probability must never read as 0%."""
        self._run(p_cancel=None)
        r = self.client.get("/dashboard/")

        self.assertContains(r, "CANCEL")
        self.assertNotContains(r, "chance of cancellation")

    def test_zero_is_still_shown(self, _q):
        self._run(recommendation="GO", p_cancel=0.0)
        r = self.client.get("/dashboard/")

        self.assertContains(r, "0% chance of cancellation")

    def test_sites_table_scrolls_inside_its_own_box(self, _q):
        self._run()
        html = self.client.get("/dashboard/").content.decode()

        self.assertIn('<div class="table-responsive">', html)
        self.assertLess(
            html.index('<div class="table-responsive">'),
            html.index('<table class="data-table">'),
        )
