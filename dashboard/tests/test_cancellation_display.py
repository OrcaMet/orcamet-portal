"""
Display of the cancellation probability on the site detail page.

The property that matters most: an unavailable probability must never render
as 0%, which would read as "certainly fine" rather than "we do not know".
"""

from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from dashboard.views import _annotate_cancellation
from forecasts.models import ForecastRun
from sites.models import Client, Site, ThresholdProfile


class AnnotateTests(TestCase):
    def _run(self, **kwargs):
        return ForecastRun(**kwargs)

    def test_probability_is_rendered_as_a_whole_percentage(self):
        run = self._run(p_cancel=0.436, p_cancel_by_variable={})
        _annotate_cancellation(run)
        self.assertEqual(run.p_cancel_pct, 44)

    def test_missing_probability_stays_none(self):
        run = self._run(p_cancel=None)
        _annotate_cancellation(run)

        self.assertIsNone(run.p_cancel_pct)
        self.assertEqual(run.p_cancel_causes, "")

    def test_zero_is_distinct_from_missing(self):
        run = self._run(p_cancel=0.0, p_cancel_by_variable={})
        _annotate_cancellation(run)
        self.assertEqual(run.p_cancel_pct, 0)

    def test_causes_are_named_in_plain_words_biggest_first(self):
        run = self._run(p_cancel=0.5, p_cancel_by_variable={
            "precip_cancel": 0.1, "gust_cancel": 0.4,
        })
        _annotate_cancellation(run)
        self.assertEqual(run.p_cancel_causes, "gusts 40%, rain 10%")

    def test_zero_contributors_are_omitted(self):
        run = self._run(p_cancel=0.4, p_cancel_by_variable={
            "gust_cancel": 0.4, "precip_cancel": 0.0,
        })
        _annotate_cancellation(run)
        self.assertEqual(run.p_cancel_causes, "gusts 40%")


@patch("sites.signals.queue_forecast_generation", return_value=True)
class SiteDetailRenderTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="Acme Rope")
        self.site = Site.objects.create(
            client=client, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )
        ThresholdProfile.objects.create(site=self.site)
        self.user = User.objects.create_user(
            username="dave", role=User.Role.CLIENT_ADMIN, client=client,
        )
        self.client.force_login(self.user)

    def _make_run(self, **kwargs):
        defaults = dict(
            site=self.site,
            forecast_date=timezone.localdate(),
            status=ForecastRun.Status.SUCCESS,
            peak_risk=12.0, recommendation="GO",
            peak_wind=5.0, peak_gust=9.0, peak_precip=0.0,
            min_temp=8.0, max_temp=14.0,
            models_used=["ukv"],
        )
        defaults.update(kwargs)
        return ForecastRun.objects.create(**defaults)

    def _page(self):
        return self.client.get(f"/dashboard/site/{self.site.pk}/").content.decode()

    def test_probability_and_causes_are_shown(self, _q):
        self._make_run(p_cancel=0.43, p_cancel_by_variable={"gust_cancel": 0.43},
                       ensemble_members=122)
        html = self._page()

        self.assertIn("43%", html)
        self.assertIn("gusts 43%", html)
        self.assertIn("Chance of cancellation", html)

    def test_unavailable_probability_says_so(self, _q):
        self._make_run(p_cancel=None)
        html = self._page()

        self.assertIn("Not available", html)
        self.assertNotIn("0%", html)

    def test_limiting_variable_is_named_next_to_the_verdict(self, _q):
        self._make_run(recommendation="CANCEL", limiting_variable="gust")
        self.assertIn("gust", self._page())

    def test_go_with_a_high_probability_shows_both(self, _q):
        """The case a supervisor most needs: calm centrally, scattered members."""
        self._make_run(recommendation="GO", p_cancel=0.38,
                       p_cancel_by_variable={"gust_cancel": 0.38})
        html = self._page()

        self.assertIn("GO", html)
        self.assertIn("38%", html)
