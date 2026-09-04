"""
Map pins are scored against their own site's limits.

Every site can carry a ThresholdProfile, and the rest of the portal scores
it against that. The map did not: it gated every marker against the single
UK-wide MapThresholds row, so a sheltered site and an exposed one got
identical verdicts on identical weather. The exposure line in the popup said
they differed while the colour said they did not, and a client who had
carefully set their own limits in the admin saw none of it here.

Also covered: the daily-summary pin now carries the chance of cancellation,
which is the quantity the default contour layer draws. The pin previously
showed only a severity score, so the marker and the field beneath it were
two different measures with nothing on screen saying so.
"""

from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db.models.signals import post_save
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from forecasts.models import ForecastRun, HourlyForecast
from sites.models import Client as SiteClient, Site, ThresholdProfile
from sites.signals import trigger_forecast_on_site_save

TEMPLATE = (
    Path(settings.BASE_DIR)
    / "dashboard" / "templates" / "dashboard" / "weather_map.html"
)


class PerSiteThresholdTests(TestCase):

    @classmethod
    def setUpClass(cls):
        post_save.disconnect(trigger_forecast_on_site_save, sender=Site)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        post_save.connect(trigger_forecast_on_site_save, sender=Site)

    def setUp(self):
        self.user = User.objects.create_user(
            username="steve", password="x", role=User.Role.SUPERADMIN,
        )
        self.client.force_login(self.user)
        self.org = SiteClient.objects.create(name="Acme Rope Access")

        self.sheltered = Site.objects.create(
            client=self.org, name="Sheltered Court", postcode="M1 1AA",
            latitude=53.48, longitude=-2.24,
        )
        self.exposed = Site.objects.create(
            client=self.org, name="Exposed Stack", postcode="AB11 5AA",
            latitude=57.14, longitude=-2.09,
        )

        ThresholdProfile.objects.create(
            site=self.exposed, is_active=True,
            wind_mean_caution=6.0, wind_mean_cancel=9.0,
            gust_caution=9.0, gust_cancel=12.0,
        )

        today = timezone.localdate()
        self.hour = timezone.now().replace(minute=0, second=0, microsecond=0)

        for site in (self.sheltered, self.exposed):
            run = ForecastRun.objects.create(
                site=site, forecast_date=today,
                status=ForecastRun.Status.SUCCESS,
                peak_risk=30.0, recommendation="GO",
                peak_wind=8.0, peak_gust=11.0, peak_precip=0.1,
                min_temp=6.0, max_temp=12.0,
                p_cancel=0.42, limiting_variable="gust",
            )
            HourlyForecast.objects.create(
                run=run, timestamp=self.hour,
                wind_speed=8.0, wind_gusts=11.0,
                precipitation=0.1, temperature=9.0, hourly_risk=30.0,
            )

    def _hourly(self):
        return self.client.get(
            reverse("dashboard:map_sites_hourly_json")
        ).json()

    def _sites(self):
        return self.client.get(reverse("dashboard:map_sites_json")).json()

    # --------------------------------------------------------
    # Per-site thresholds reach the client
    # --------------------------------------------------------

    def test_the_hourly_payload_carries_site_thresholds(self):
        data = self._hourly()

        self.assertIn("thresholds", data)
        self.assertIn(str(self.exposed.id), data["thresholds"])

    def test_a_sites_own_limits_are_the_ones_sent(self):
        thresholds = self._hourly()["thresholds"][str(self.exposed.id)]

        self.assertEqual(thresholds["gust_cancel"], 12.0)
        self.assertEqual(thresholds["wind_mean_caution"], 6.0)

    def test_a_site_without_a_profile_is_absent(self):
        """Absence is the signal to fall back to the UK-wide row."""
        self.assertNotIn(str(self.sheltered.id), self._hourly()["thresholds"])

    def test_the_two_sites_would_score_differently(self):
        """
        The point of the fix. Identical weather (11 m/s gusts), one site with
        a 12 m/s cancel limit and one falling back to the global default —
        the payload must be able to tell them apart.
        """
        thresholds = self._hourly()["thresholds"]

        self.assertIn(str(self.exposed.id), thresholds)
        self.assertNotIn(str(self.sheltered.id), thresholds)

    def test_thresholds_are_sent_once_not_per_feature(self):
        """
        Repeating ten numbers for every site in every one of 72 frames would
        be most of the payload.
        """
        data = self._hourly()
        feature = data["frames"][data["timestamps"][0]]["features"][0]

        self.assertNotIn("thresholds", feature["properties"])
        self.assertNotIn("gust_cancel", feature["properties"])

    # --------------------------------------------------------
    # Cancellation chance on the summary pin
    # --------------------------------------------------------

    def test_the_summary_pin_carries_the_cancellation_chance(self):
        props = self._sites()["features"][0]["properties"]

        self.assertEqual(props["p_cancel"], 42)

    def test_it_carries_what_limited_the_day(self):
        props = self._sites()["features"][0]["properties"]

        self.assertEqual(props["limiting_variable"], "gust")

    def test_an_unknown_chance_stays_null(self):
        """A null must never render as 0%, which reads as certainly fine."""
        ForecastRun.objects.update(p_cancel=None)

        props = self._sites()["features"][0]["properties"]

        self.assertIsNone(props["p_cancel"])


class ScoringTemplateTests(TestCase):
    """
    Guard the client-side scoring path.

    Template-source assertions, as elsewhere in this suite — there is no JS
    runtime here — so deliberately narrow.
    """

    def setUp(self):
        self.source = TEMPLATE.read_text(encoding="utf-8")

    def test_the_scorers_take_a_threshold_set(self):
        self.assertIn("function verdictOf(w,g,p,t,th)", self.source)
        self.assertIn("function cRisk(w,g,p,t,th)", self.source)

    def test_a_lookup_helper_exists(self):
        self.assertIn("function thresholdsFor(", self.source)

    def test_the_hourly_markers_use_the_sites_own_limits(self):
        self.assertIn("thresholdsFor(p.id)", self.source)

    def test_the_payloads_thresholds_are_stored(self):
        self.assertIn("siteTH = d.thresholds", self.source)

    def test_the_severity_score_is_no_longer_called_risk_alone(self):
        """Two different measures sharing the word 'risk' is how they got
        conflated with the cancellation contour beneath them."""
        self.assertIn("Risk score", self.source)

    def test_the_summary_popup_shows_cancellation(self):
        self.assertIn("Cancellation", self.source)
        self.assertIn("p.p_cancel", self.source)
