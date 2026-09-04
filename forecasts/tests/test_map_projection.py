"""
The contour overlay must be rendered in the projection Leaflet draws in.

L.imageOverlay stretches the PNG linearly across its bounding box in Web
Mercator. The renderer used to sample and plot linearly in latitude — an
equirectangular image — and the mismatch moved every contour feature north:
+19 km at 52N, +25.5 km at 53.5N, peaking at 26.3 km near 54.3N, back to
+7.6 km at 58N.

That is not a cosmetic error. Site markers are placed by Leaflet from their
true coordinates, so the colour under a pin was the weather from roughly
25 km south of it, and the hover readout — which reads the stored grid
coordinates — disagreed with the colour beneath the cursor.

These tests pin the mapping from latitude to image row. The displacement
they guard against is largest in the middle of the domain and vanishes at
both ends, so a test at one latitude alone would not have caught it.
"""

from unittest import TestCase
from unittest.mock import patch

import numpy as np

from forecasts.engine import map_interpolation as mi
from forecasts.engine.map_interpolation import (
    UK_LAT_MAX,
    UK_LAT_MIN,
    UK_LON_MAX,
    UK_LON_MIN,
    interpolate_risk_surface,
    inverse_mercator_y,
    mercator_y,
    render_contour_to_bytes,
)

KM_PER_DEGREE_LAT = 111.32

# The rendered row for a latitude must land within this of where Leaflet
# will read it. The old defect was 26 km at its worst.
TOLERANCE_KM = 1.0


def _grid(spacing=0.5):
    """A complete grid covering the render bounds, valued by latitude."""
    lats, lons = np.meshgrid(
        np.arange(UK_LAT_MIN, UK_LAT_MAX + spacing, spacing),
        np.arange(UK_LON_MIN, UK_LON_MAX + spacing, spacing),
    )
    lats, lons = lats.ravel(), lons.ravel()
    return lats, lons, lats.copy()


class MercatorHelperTests(TestCase):

    def test_round_trip(self):
        lats = np.linspace(UK_LAT_MIN, UK_LAT_MAX, 40)
        back = inverse_mercator_y(mercator_y(lats))
        self.assertTrue(np.allclose(back, lats))

    def test_it_is_not_the_identity_over_the_uk(self):
        """A no-op transform would pass every other test here."""
        lo, hi = mercator_y(UK_LAT_MIN), mercator_y(UK_LAT_MAX)
        mid_lat = float(inverse_mercator_y((lo + hi) / 2.0))
        linear_mid = (UK_LAT_MIN + UK_LAT_MAX) / 2.0

        self.assertGreater(
            abs(mid_lat - linear_mid) * KM_PER_DEGREE_LAT, 20.0,
            "Mercator and linear latitude should differ by tens of km here",
        )


class RenderGridTests(TestCase):
    """The interpolated rows are the image's rows."""

    def setUp(self):
        lats, lons, vals = _grid()
        self.glons, self.glats, _ = interpolate_risk_surface(
            lats, lons, vals, resolution=120
        )
        self.rows = self.glats[:, 0]

    def test_rows_span_the_render_bounds(self):
        self.assertAlmostEqual(float(self.rows[0]), UK_LAT_MIN, places=6)
        self.assertAlmostEqual(float(self.rows[-1]), UK_LAT_MAX, places=6)

    def test_rows_are_evenly_spaced_in_mercator_y(self):
        steps = np.diff(mercator_y(self.rows))
        self.assertTrue(
            np.allclose(steps, steps[0]),
            "image rows are not uniform in the projection they are drawn in",
        )

    def test_rows_are_not_evenly_spaced_in_latitude(self):
        """The old behaviour, stated so a revert fails loudly."""
        steps = np.diff(self.rows)
        self.assertFalse(np.allclose(steps, steps[0]))

    def test_every_row_lands_where_leaflet_will_read_it(self):
        """
        Row i occupies fraction i/(n-1) of the image. Leaflet reads that
        fraction as a position in Mercator y between the bounds, so the row's
        latitude must be the one at that Mercator fraction.
        """
        n = len(self.rows)
        y_min, y_max = mercator_y(UK_LAT_MIN), mercator_y(UK_LAT_MAX)

        worst = 0.0
        for i, lat in enumerate(self.rows):
            fraction = i / (n - 1)
            shown = float(inverse_mercator_y(y_min + fraction * (y_max - y_min)))
            worst = max(worst, abs(shown - float(lat)) * KM_PER_DEGREE_LAT)

        self.assertLess(worst, TOLERANCE_KM, f"worst displacement {worst:.1f} km")

    def test_longitude_is_still_linear(self):
        """Mercator is linear in longitude; only y needed changing."""
        cols = self.glons[0, :]
        steps = np.diff(cols)
        self.assertTrue(np.allclose(steps, steps[0]))
        self.assertAlmostEqual(float(cols[0]), UK_LON_MIN, places=6)
        self.assertAlmostEqual(float(cols[-1]), UK_LON_MAX, places=6)


class RenderedAxesTests(TestCase):
    """
    The figure's y axis must be in Mercator y too.

    Uniform rows would still be drawn wrongly if the axis they were plotted
    against ran linearly in latitude, so this checks the limits the renderer
    actually sets rather than trusting the grid alone.
    """

    def test_the_y_axis_is_in_mercator(self):
        lats, lons, vals = _grid()
        captured = []

        real_subplots = mi.plt.subplots

        def spy(*args, **kwargs):
            fig, ax = real_subplots(*args, **kwargs)
            captured.append(ax)
            return fig, ax

        with patch.object(mi.plt, "subplots", side_effect=spy):
            render_contour_to_bytes(lats, lons, vals, variable="pcancel")

        self.assertTrue(captured, "renderer never created a figure")
        low, high = captured[-1].get_ylim()

        self.assertAlmostEqual(low, float(mercator_y(UK_LAT_MIN)), places=6)
        self.assertAlmostEqual(high, float(mercator_y(UK_LAT_MAX)), places=6)

    def test_a_png_is_still_produced(self):
        lats, lons, vals = _grid()
        png = render_contour_to_bytes(lats, lons, vals, variable="pcancel")
        self.assertTrue(png.startswith(b"\x89PNG"))
