"""
CARTO basemap key wiring.

CARTO moved their basemaps behind an API key and now serves tiles with a
diagonal "API KEY REQUIRED" watermark painted into every image — a valid
200 response, so nothing in the app noticed. The key is supplied through
the environment and injected into the map page.

It is a client-side credential by nature (it ships in the page), so these
tests care about correctness and escaping, not secrecy.
"""

from urllib.parse import parse_qs, urlparse

from django.db.models.signals import post_save
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from dashboard.views import CARTO_BASEMAP_TEMPLATE, basemap_url
from sites.models import Client as SiteClient, Site
from sites.signals import trigger_forecast_on_site_save


class BasemapUrlTests(TestCase):

    @override_settings(CARTO_API_KEY="")
    def test_without_a_key_the_plain_url_is_used(self):
        """A missing key must cost a watermark, not a broken map."""
        self.assertEqual(basemap_url(), CARTO_BASEMAP_TEMPLATE)
        self.assertNotIn("api_key", basemap_url())

    @override_settings(CARTO_API_KEY="abc123")
    def test_with_a_key_it_is_appended(self):
        url = basemap_url()

        self.assertTrue(url.startswith(CARTO_BASEMAP_TEMPLATE))
        self.assertEqual(
            parse_qs(urlparse(url).query)["api_key"], ["abc123"]
        )

    @override_settings(CARTO_API_KEY="abc123")
    def test_leaflet_placeholders_survive(self):
        """Leaflet needs {s}/{z}/{x}/{y}/{r} intact to build tile URLs."""
        url = basemap_url()

        for token in ("{s}", "{z}", "{x}", "{y}", "{r}"):
            self.assertIn(token, url)

    @override_settings(CARTO_API_KEY="a b&c=d")
    def test_the_key_is_url_encoded(self):
        """A key with reserved characters must not corrupt the query string."""
        url = basemap_url()

        self.assertNotIn("a b&c=d", url)
        self.assertEqual(
            parse_qs(urlparse(url).query)["api_key"], ["a b&c=d"]
        )


class BasemapPageTests(TestCase):

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
        SiteClient.objects.create(name="Acme Rope Access")
        self.client.force_login(self.user)

    @override_settings(CARTO_API_KEY="page-key-123")
    def test_the_key_reaches_the_map_page(self):
        resp = self.client.get(reverse("dashboard:weather_map"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "basemap-url")
        self.assertContains(resp, "page-key-123")

    @override_settings(CARTO_API_KEY="")
    def test_the_page_still_renders_without_a_key(self):
        resp = self.client.get(reverse("dashboard:weather_map"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "basemap-url")

    @override_settings(CARTO_API_KEY='"><script>alert(1)</script>')
    def test_a_hostile_key_cannot_break_out_of_the_script_tag(self):
        """
        json_script escapes the value; a raw interpolation would not.

        Guarding this because the key arrives from the environment and is
        written straight into a <script> block.
        """
        resp = self.client.get(reverse("dashboard:weather_map"))
        body = resp.content.decode()

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("<script>alert(1)</script>", body)

    def test_the_template_no_longer_hardcodes_the_tile_url(self):
        """The URL must come from the view, or the key can never apply."""
        from pathlib import Path

        from django.conf import settings

        source = (
            Path(settings.BASE_DIR)
            / "dashboard" / "templates" / "dashboard" / "weather_map.html"
        ).read_text(encoding="utf-8")

        self.assertIn("BASEMAP_URL", source)
        self.assertNotIn("L.tileLayer('https://", source)
