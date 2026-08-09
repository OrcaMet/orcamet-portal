"""
OrcaMet Portal — Integration tests for the weather map endpoints.

These exercise the real URL routing, permission filtering and JSON encoding
for the map, and pin the audit fixes end to end:

* a site on the Greenwich meridian (longitude 0.0) appears on the map
* every map response is strictly valid JSON
* the map degrades gracefully when no UK risk grid run exists
* the legacy /forecasts/risk-map/ route redirects instead of raising a 500
* an unknown contour variable is a 404, not a 500
"""

import json

from django.db.models.signals import post_save
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from forecasts.models import ForecastRun, HourlyForecast, UKRiskGridRun
from sites.models import Client as SiteClient, Site
from sites.signals import trigger_forecast_on_site_save


def _strict_loads(body):
    """Parse JSON, rejecting the non-standard NaN/Infinity tokens."""
    def reject(value):
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(body, parse_constant=reject)


class MapEndpointTests(TestCase):

    @classmethod
    def setUpClass(cls):
        # Saving a Site would otherwise trigger live forecast generation.
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
        self.org = SiteClient.objects.create(name="Acme Rope Access")

        # Longitude exactly 0.0 — the Greenwich meridian case.
        self.greenwich = Site.objects.create(
            client=self.org, name="Greenwich Tower", postcode="SE10 9NF",
            latitude=51.4779, longitude=0.0,
        )
        # A normal negative-longitude site for contrast.
        self.bristol = Site.objects.create(
            client=self.org, name="Bristol Stack", postcode="BS1 4DJ",
            latitude=51.4545, longitude=-2.5879,
        )

        self.client.force_login(self.user)

    # --------------------------------------------------------
    # Greenwich meridian
    # --------------------------------------------------------

    def test_zero_longitude_site_appears_in_sites_json(self):
        resp = self.client.get(reverse("dashboard:map_sites_json"))
        self.assertEqual(resp.status_code, 200)

        data = _strict_loads(resp.content)
        names = {f["properties"]["name"] for f in data["features"]}

        self.assertIn("Greenwich Tower", names)
        self.assertIn("Bristol Stack", names)
        self.assertEqual(len(data["features"]), 2)

    def test_zero_longitude_coordinates_are_preserved(self):
        resp = self.client.get(reverse("dashboard:map_sites_json"))
        data = _strict_loads(resp.content)

        feature = next(
            f for f in data["features"]
            if f["properties"]["name"] == "Greenwich Tower"
        )
        self.assertEqual(feature["geometry"]["coordinates"], [0.0, 51.4779])

    # --------------------------------------------------------
    # JSON validity
    # --------------------------------------------------------

    def test_all_map_endpoints_return_strict_json(self):
        run = ForecastRun.objects.create(
            site=self.bristol,
            forecast_date="2026-08-09",
            status=ForecastRun.Status.SUCCESS,
            peak_risk=42.0, recommendation="CAUTION",
            peak_wind=12.0, peak_gust=18.0, peak_precip=0.4, min_temp=3.0,
        )
        HourlyForecast.objects.create(
            run=run, timestamp="2026-08-09T09:00:00Z",
            wind_speed=12.0, wind_gusts=18.0,
            precipitation=0.4, temperature=3.0, hourly_risk=42.0,
        )

        for name in (
            "dashboard:map_sites_json",
            "dashboard:map_sites_hourly_json",
            "dashboard:map_contour_timestamps",
        ):
            with self.subTest(endpoint=name):
                resp = self.client.get(reverse(name))
                self.assertEqual(resp.status_code, 200)
                _strict_loads(resp.content)  # raises if NaN/Infinity present

    # --------------------------------------------------------
    # Graceful degradation with no grid run
    # --------------------------------------------------------

    def test_timestamps_endpoint_without_grid_run(self):
        self.assertFalse(UKRiskGridRun.objects.exists())

        resp = self.client.get(reverse("dashboard:map_contour_timestamps"))
        self.assertEqual(resp.status_code, 200)

        data = _strict_loads(resp.content)
        self.assertFalse(data["available"])
        self.assertEqual(data["timestamps"], [])

    def test_weather_map_page_renders_without_grid_run(self):
        resp = self.client.get(reverse("dashboard:weather_map"))
        self.assertEqual(resp.status_code, 200)

    def test_contour_image_without_grid_run_is_404(self):
        resp = self.client.get(reverse("dashboard:map_contour_image"))
        self.assertEqual(resp.status_code, 404)

    # --------------------------------------------------------
    # Input validation
    # --------------------------------------------------------

    def test_unknown_contour_variable_is_404(self):
        resp = self.client.get(
            reverse("dashboard:map_contour_image"), {"var": "definitely-not-a-variable"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_non_numeric_run_key_does_not_error(self):
        """The client falls back to a timestamp string for the run key."""
        resp = self.client.get(
            reverse("dashboard:map_contour_image"),
            {"var": "risk", "run": "2026-08-09T00:00:00+00:00"},
        )
        # No grid run exists, so 404 — the point is that it is not a 500.
        self.assertEqual(resp.status_code, 404)

    # --------------------------------------------------------
    # Legacy route
    # --------------------------------------------------------

    def test_legacy_risk_map_route_redirects(self):
        resp = self.client.get(reverse("forecasts:risk_map_detail"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("dashboard:weather_map"))


class MapPermissionTests(TestCase):
    """Client users must only see their own organisation's sites."""

    @classmethod
    def setUpClass(cls):
        post_save.disconnect(trigger_forecast_on_site_save, sender=Site)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        post_save.connect(trigger_forecast_on_site_save, sender=Site)

    def test_client_user_sees_only_their_sites(self):
        org_a = SiteClient.objects.create(name="Org A")
        org_b = SiteClient.objects.create(name="Org B")

        Site.objects.create(client=org_a, name="A Site", postcode="X",
                            latitude=51.0, longitude=0.0)
        Site.objects.create(client=org_b, name="B Site", postcode="Y",
                            latitude=52.0, longitude=-1.0)

        user = User.objects.create_user(
            username="clientuser", password="x",
            role=User.Role.CLIENT_USER, client=org_a,
        )
        self.client.force_login(user)

        data = _strict_loads(
            self.client.get(reverse("dashboard:map_sites_json")).content
        )
        names = {f["properties"]["name"] for f in data["features"]}

        self.assertEqual(names, {"A Site"})

    def test_anonymous_user_is_redirected_to_login(self):
        resp = self.client.get(reverse("dashboard:map_sites_json"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])
