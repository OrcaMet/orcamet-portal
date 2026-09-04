"""
The map paints its own field now, instead of showing a server PNG.

Five variables x 72 forecast hours of matplotlib renders per run, stored as
BLOBs and fetched one per frame, replaced by painting the values the hover
readout already fetches. That also retires a whole class of defect: the
field is drawn in the map's own coordinates, so there is no second
projection that can drift out of step with Leaflet's — which is what put
every contour feature up to 26 km north of where it belonged.

Two properties of the server renderer had to survive the move, and they are
what these tests are mostly about. A gap in the grid must not be painted
across, because a rate-limited run can lose whole latitude bands and
inventing weather over one is worse than showing nothing. And the surface
must not exceed the values it was built from.

The renderer itself is JavaScript, so its arithmetic is pinned in
orcamet_portal/static/orcamet_portal/js/field-renderer.js and asserted on
here at the level the suite can reach: that the page loads it, that the
values it needs are actually shipped, and that the PNG path is no longer in
the way.
"""

from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from forecasts.models import UKRiskGridPoint, UKRiskGridRun

TEMPLATE = (
    Path(settings.BASE_DIR)
    / "dashboard" / "templates" / "dashboard" / "weather_map.html"
)
RENDERER = (
    Path(settings.BASE_DIR)
    / "orcamet_portal" / "static" / "orcamet_portal" / "js"
    / "field-renderer.js"
)


class RendererShippedTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username="steve", password="x", role=User.Role.SUPERADMIN,
        )
        self.client.force_login(self.user)
        self.source = TEMPLATE.read_text(encoding="utf-8")

    def test_the_renderer_file_exists(self):
        self.assertTrue(RENDERER.exists(), RENDERER)

    def test_the_page_loads_it(self):
        self.assertIn("field-renderer.js", self.source)

    def test_the_page_carries_the_colour_ramps(self):
        response = self.client.get(reverse("dashboard:weather_map"))

        self.assertContains(response, 'id="map-colours"')

    def test_the_ramps_are_json_not_markup(self):
        """json_script, so a colour table cannot break out of the tag."""
        self.assertIn('{{ map_colours|json_script:"map-colours" }}', self.source)


class PngPathRetiredTests(TestCase):
    """The image overlay is gone; nothing should still reach for it."""

    def setUp(self):
        self.source = TEMPLATE.read_text(encoding="utf-8")

    def test_no_image_overlay_remains(self):
        self.assertNotIn("L.imageOverlay", self.source)

    def test_the_contour_endpoint_is_no_longer_called(self):
        self.assertNotIn("map_contour_image", self.source)

    def test_the_image_preloader_is_gone(self):
        for name in ("preloadImage", "preloadAllFrames", "loadContourImage"):
            with self.subTest(name=name):
                self.assertNotIn(name, self.source)

    def test_the_field_layer_replaced_it(self):
        self.assertIn("var FieldLayer", self.source)
        self.assertIn("OrcaMetField.paint", self.source)
        self.assertIn("OrcaMetField.buildLattice", self.source)


class RendererInvariantTests(TestCase):
    """
    The two properties carried over from the server renderer. Asserted on
    the source, since the suite has no JS runtime — narrow, but these are
    the lines whose removal would quietly make the map dishonest.
    """

    def setUp(self):
        self.js = RENDERER.read_text(encoding="utf-8")

    def test_a_hole_is_not_interpolated_across(self):
        """
        Bilinear sampling returns NaN when any surrounding cell is missing,
        rather than falling back to the nearest one.
        """
        block = self.js.split("function sample")[1].split("function colourFor")[0]

        self.assertIn("isNaN(v00) || isNaN(v10) || isNaN(v01) || isNaN(v11)", block)
        self.assertIn("return NaN", block)

    def test_the_lattice_pitch_is_the_minimum_gap(self):
        """
        Deriving it from the average gap silently squeezes out missing rows
        and displaces everything above them — the defect this was written
        against.
        """
        block = self.js.split("function latticeStep")[1].split("}")[0]

        self.assertIn("<", block)
        self.assertNotIn("length - 1", block)

    def test_the_gap_tolerance_matches_the_server(self):
        """
        MAX_GAP_CELLS mirrors MAX_GAP_FACTOR in map_interpolation.py, so the
        client blanks the same voids the server did.
        """
        from forecasts.engine.map_interpolation import MAX_GAP_FACTOR

        self.assertIn(
            "var MAX_GAP_CELLS = %s;" % MAX_GAP_FACTOR, self.js,
        )

    def test_it_paints_per_row_and_column_not_per_pixel(self):
        """
        A per-pixel projection callback made a full-screen paint take
        roughly 470 ms; per row and column it is under 60. That is the
        difference between a timeline that scrubs and one that stutters.
        """
        self.assertIn("latForRow, lonForCol", self.js)

    def test_bilinear_keeps_the_surface_bounded(self):
        """
        The server used a cubic and then clamped the overshoot away. A
        bilinear blend of four corners cannot leave their range at all, so
        there is nothing to clamp.
        """
        block = self.js.split("function sample")[1].split("function colourFor")[0]

        self.assertIn("(1 - tx) * (1 - ty)", block)


class GridPointPayloadTests(TestCase):
    """
    The field is painted from this payload now, not just hovered over, so
    its shape matters more than it did.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="steve", password="x", role=User.Role.SUPERADMIN,
        )
        self.client.force_login(self.user)

        run = UKRiskGridRun.objects.create(
            forecast_date=timezone.localdate(),
            status=UKRiskGridRun.Status.SUCCESS,
        )
        self.hour = timezone.now().replace(minute=0, second=0, microsecond=0)

        for lat in (54.0, 54.5):
            for lon in (-3.0, -2.5):
                UKRiskGridPoint.objects.create(
                    run=run, latitude=lat, longitude=lon, timestamp=self.hour,
                    wind_speed=8.0, wind_gusts=13.0, precipitation=0.3,
                    temperature=9.0, risk=25.0, p_cancel=18.0,
                    wind_direction=225.0, wind_direction_agreement=0.9,
                )

    def test_every_variable_the_map_draws_is_present(self):
        payload = self.client.get(
            reverse("dashboard:map_grid_points_json")
        ).json()
        row = payload["points"][0]

        # risk, wind, gust, precip, temp, pcancel — the six the renderer
        # has a colour ramp for.
        for index in (2, 3, 4, 5, 6, 7):
            with self.subTest(index=index):
                self.assertIsNotNone(row[index])

    def test_the_lattice_is_recoverable_from_the_rows(self):
        """
        The renderer rebuilds a regular grid by rounding coordinates onto a
        pitch, so the coordinates have to come back with enough precision to
        land on it.
        """
        payload = self.client.get(
            reverse("dashboard:map_grid_points_json")
        ).json()

        lats = sorted({p[0] for p in payload["points"]})
        lons = sorted({p[1] for p in payload["points"]})

        self.assertEqual(lats, [54.0, 54.5])
        self.assertEqual(lons, [-3.0, -2.5])
