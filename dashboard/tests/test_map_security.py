"""
Regression tests for the weather map's popup rendering and thresholds.

Covers two defects found in audit:

  1. Popups are built as HTML strings and parsed by Leaflet's bindPopup, so
     an unescaped site name ran script in the viewer's session. A superadmin
     sees every site on this map, and trial accounts can name their own
     sites, so the payload did not have to come from staff.

  2. The client-side risk calculation used thresholds hardcoded in the
     template, so editing them in the admin moved the contour layer but left
     the site markers scoring against the old numbers.
"""

import json
import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from forecasts.models import MapThresholds
from sites.models import Client, Site

TEMPLATE = (
    Path(settings.BASE_DIR) / "dashboard" / "templates" / "dashboard" / "weather_map.html"
)

PAYLOAD = '<img src=x onerror=alert(document.domain)>'


class MapPopupEscapingTests(TestCase):
    def setUp(self):
        self.source = TEMPLATE.read_text(encoding="utf-8")

    def test_user_controlled_fields_are_escaped_in_popup_markup(self):
        """
        Every interpolation of a user-entered field into popup HTML must go
        through esc(). Reads the template source because the defect lives in
        JavaScript, which the Python suite cannot execute.
        """
        unescaped = []
        for field in ("name", "client", "postcode", "exposure"):
            # Matches `p.name` only when not already inside esc(...).
            for match in re.finditer(rf"(\w*\(?)\s*p\.{field}\b", self.source):
                if "esc(" not in match.group(0):
                    line = self.source[: match.start()].count("\n") + 1
                    unescaped.append(f"p.{field} at line {line}")

        self.assertEqual(
            unescaped, [],
            "user-entered fields interpolated into popup HTML without esc(): "
            + ", ".join(unescaped),
        )

    def test_escape_helper_covers_the_dangerous_characters(self):
        self.assertIn("var ESC_MAP", self.source)
        table = self.source.split("var ESC_MAP")[1][:300]

        # Assert on the entities produced rather than the key quoting, which
        # differs for the apostrophe.
        for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            self.assertIn(entity, table)


class MapSitesJsonTests(TestCase):
    """The JSON itself is correct — the flaw was in how the client used it."""

    def setUp(self):
        tester = Client.objects.create(name="Tester Sandbox", is_sandbox=True)
        Site.objects.create(
            client=tester, name=PAYLOAD, postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )
        self.admin = User.objects.create_user(
            username="steve", role=User.Role.SUPERADMIN,
        )
        self.client.force_login(self.admin)

    def test_superadmin_sees_every_clients_site(self):
        """Establishes the reach: one tester's name lands in Steve's browser."""
        resp = self.client.get("/dashboard/map/sites.json")
        names = [f["properties"]["name"] for f in resp.json()["features"]]
        self.assertEqual(names, [PAYLOAD])

    def test_dashboard_table_escapes_the_same_value(self):
        """Django templates were never the problem; this pins that down."""
        html = self.client.get("/dashboard/").content.decode()
        self.assertNotIn(PAYLOAD, html)
        self.assertIn("&lt;img src=x", html)


class MapThresholdsWiringTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="Acme Rope")
        self.user = User.objects.create_user(
            username="dave", role=User.Role.CLIENT_ADMIN, client=client,
        )
        self.client.force_login(self.user)

    def _rendered_thresholds(self):
        html = self.client.get("/dashboard/map/").content.decode()
        match = re.search(
            r'<script id="map-thresholds" type="application/json">(.*?)</script>',
            html, re.S,
        )
        self.assertIsNotNone(match, "map thresholds were not rendered into the page")
        return json.loads(match.group(1))

    def test_page_carries_the_admin_thresholds(self):
        self.assertEqual(self._rendered_thresholds(), MapThresholds.load().as_dict())

    def test_editing_in_the_admin_changes_what_the_map_scores_against(self):
        obj = MapThresholds.load()
        obj.gust_caution = 9.0
        obj.gust_cancel = 13.0
        obj.full_clean()
        obj.save()

        rendered = self._rendered_thresholds()

        self.assertEqual(rendered["gust_caution"], 9.0)
        self.assertEqual(rendered["gust_cancel"], 13.0)

    def test_heat_thresholds_reach_the_map(self):
        """Heat was absent from the template's hardcoded set entirely."""
        rendered = self._rendered_thresholds()
        self.assertEqual(rendered["temp_max_caution"], 27.0)
        self.assertEqual(rendered["temp_max_cancel"], 32.0)

    def test_blank_heat_survives_as_null(self):
        obj = MapThresholds.load()
        obj.temp_max_caution = None
        obj.temp_max_cancel = None
        obj.save()

        self.assertIsNone(self._rendered_thresholds()["temp_max_caution"])

    def test_template_no_longer_hardcodes_thresholds(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        crisk = source.split("function cRisk")[1].split("}")[0]

        self.assertNotIn("10,14", crisk.replace(" ", ""))
        self.assertNotIn("15,20", crisk.replace(" ", ""))

        # The scorer now takes the threshold set to score against, so a site
        # can be gated by its own ThresholdProfile. It reads every limit off
        # that argument, and falls back to the admin-provided TH when a site
        # has no profile of its own — which is what this originally guarded.
        self.assertIn("th.wind_mean_caution", crisk)
        self.assertIn("th.gust_caution", crisk)
        self.assertIn("th = th || TH", crisk)


class GridPointsPayloadTests(TestCase):
    """
    The map reads grid values by array position, so the column order in
    map/grid-points.json is a contract with FIELD_FOR_VAR in the template.
    """

    def setUp(self):
        from forecasts.models import UKRiskGridPoint, UKRiskGridRun

        client = Client.objects.create(name="Acme Rope")
        self.user = User.objects.create_user(
            username="dave", role=User.Role.CLIENT_ADMIN, client=client,
        )
        self.client.force_login(self.user)

        run = UKRiskGridRun.objects.create(
            forecast_date=timezone.localdate(),
            status=UKRiskGridRun.Status.SUCCESS,
            lat_min=49.9, lat_max=58.7, lon_min=-7.6, lon_max=1.8,
        )
        UKRiskGridPoint.objects.create(
            run=run, latitude=55.0, longitude=-3.0,
            timestamp=timezone.now(),
            wind_speed=5.0, wind_gusts=9.0, precipitation=0.2,
            temperature=11.0, risk=14.0, p_cancel=37.5, ensemble_members=51,
        )

    def test_cancellation_is_the_eighth_column(self):
        payload = self.client.get("/dashboard/map/grid-points.json").json()
        row = payload["points"][0]

        self.assertEqual(len(row), 8)
        self.assertEqual(row[7], 37.5)

    def test_template_reads_cancellation_from_that_index(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        match = re.search(r"pcancel:\s*\{idx:\s*(\d+)", source)

        self.assertIsNotNone(match, "map does not expose a cancellation layer")
        self.assertEqual(int(match.group(1)), 7)

    def test_default_layer_is_cancellation(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("var curVar = 'pcancel';", source)

    def test_site_markers_use_the_hard_gate_not_the_severity_score(self):
        """recOf() cannot represent a breach; verdictOf() can."""
        source = TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("function verdictOf(", source)
        self.assertNotIn("recOf(risk)", source)
