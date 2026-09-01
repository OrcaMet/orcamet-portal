"""
Interpolation must not invent data across gaps.

A rate-limited run left five contiguous latitude rows missing — a 3 degree
void across Scotland, with real data at 55.9N and again at 58.9N. At
longitude -3.1 both edges read ~42% chance of cancellation, and the map
rendered the space between them as a saturated 100%: the most alarming
colour on the scale, over ground where nothing had been measured.

Two causes, both covered here:

* CloughTocher2D is a C1 cubic, unbounded by its inputs, and the sliver
  triangles a long gap produces give it unstable gradients to work from.
* The renderer clipped to the colour scale's fixed range rather than to the
  observed values, so the overshoot saturated instead of being caught.
"""

from unittest import TestCase

import numpy as np

from forecasts.engine.map_interpolation import (
    MAX_GAP_FACTOR,
    interpolate_risk_surface,
    render_contour_to_bytes,
)

SPACING = 0.5
LONS = np.round(np.arange(-7.6, 1.95, SPACING), 4)

# The rows the live run actually lost.
MISSING_ROWS = {56.4, 56.9, 57.4, 57.9, 58.4}


def _grid(with_void=True, edge_value=42.0, body_value=6.0):
    """Build the failing grid: ~42% either side of a 3 degree hole."""
    lats, lons, vals = [], [], []
    for lat in np.round(np.arange(49.9, 58.95, SPACING), 4):
        if with_void and float(lat) in MISSING_ROWS:
            continue
        for lon in LONS:
            lats.append(float(lat))
            lons.append(float(lon))
            # Both edges of the void sit at the same elevated value, so any
            # honest surface between them stays near it.
            edge = float(lat) in (55.9, 58.9)
            vals.append(edge_value if edge else body_value)
    return np.array(lats), np.array(lons), np.array(vals)


class VoidBlankingTests(TestCase):

    def setUp(self):
        self.lats, self.lons, self.vals = _grid()
        self.glons, self.glats, self.surface = interpolate_risk_surface(
            self.lats, self.lons, self.vals, resolution=120
        )

    def test_the_void_is_blank_not_invented(self):
        """The centre of the 3 degree hole must be NaN."""
        centre = (self.glats > 57.0) & (self.glats < 57.8)
        self.assertTrue(centre.any(), "test grid missed the void")
        self.assertTrue(
            np.all(np.isnan(self.surface[centre])),
            "surface was drawn across a void with no observations",
        )

    def test_areas_with_data_are_still_drawn(self):
        """Blanking must not eat the parts of the map that do have data."""
        south = (self.glats > 51.0) & (self.glats < 54.0)
        self.assertTrue(np.all(np.isfinite(self.surface[south])))

    def test_the_surface_never_exceeds_what_was_observed(self):
        """The 100% blob was a cubic overshoot; it must be impossible now."""
        finite = self.surface[np.isfinite(self.surface)]

        self.assertLessEqual(finite.max(), self.vals.max() + 1e-9)
        self.assertGreaterEqual(finite.min(), self.vals.min() - 1e-9)

    def test_the_specific_regression(self):
        """
        42% on both sides of the void must never render as ~100% between.

        This is the observed failure, reduced to an assertion.
        """
        band = (self.glats > 56.2) & (self.glats < 58.6)
        values = self.surface[band]
        finite = values[np.isfinite(values)]

        # Whatever survives blanking near the edges must stay near 42, not
        # climb toward the top of the scale.
        if finite.size:
            self.assertLess(finite.max(), 50.0)

    def test_edges_are_feathered_not_cliffed(self):
        """One spacing of interpolation past the last row is still drawn."""
        just_inside = (self.glats > 56.0) & (self.glats < 56.3)
        values = self.surface[just_inside]

        self.assertTrue(np.isfinite(values).any(),
                        "blanking was too aggressive at the void edge")


class NoVoidTests(TestCase):
    """A complete grid must be unaffected."""

    def test_a_full_grid_is_not_blanked(self):
        lats, lons, vals = _grid(with_void=False)
        _, _, surface = interpolate_risk_surface(lats, lons, vals, resolution=120)

        self.assertFalse(
            np.isnan(surface).any(),
            "blanking removed cells from a grid with no gaps",
        )

    def test_blanking_can_be_disabled(self):
        lats, lons, vals = _grid()
        _, _, surface = interpolate_risk_surface(
            lats, lons, vals, resolution=120, max_distance=0
        )

        self.assertFalse(np.isnan(surface).any())


class ThresholdTests(TestCase):

    def test_threshold_scales_with_grid_spacing(self):
        """
        A coarser --resolution must not blank the whole map.

        The threshold is a multiple of the grid's own measured spacing, so a
        1 degree grid tolerates proportionally longer spans.
        """
        lats, lons, vals = [], [], []
        for lat in np.arange(50.0, 58.1, 1.0):
            for lon in np.arange(-7.0, 1.1, 1.0):
                lats.append(float(lat))
                lons.append(float(lon))
                vals.append(10.0)

        glons, glats, surface = interpolate_risk_surface(
            np.array(lats), np.array(lons), np.array(vals), resolution=120
        )

        # Interior only: this coarse grid stops short of the render bounds,
        # so its corners are outside the convex hull and are NaN for reasons
        # that have nothing to do with blanking.
        interior = (
            (glats > 51.0) & (glats < 57.0)
            & (glons > -6.0) & (glons < 0.0)
        )

        self.assertFalse(
            np.isnan(surface[interior]).any(),
            "a complete 1 degree grid should not be blanked",
        )

    def test_factor_is_greater_than_one_spacing(self):
        """Below 1.0 the threshold would blank cells between real points."""
        self.assertGreater(MAX_GAP_FACTOR, 1.0)


class RenderTests(TestCase):

    def test_a_void_still_renders_a_valid_png(self):
        lats, lons, vals = _grid()

        png = render_contour_to_bytes(lats, lons, vals, variable="pcancel")

        self.assertGreater(len(png), 0)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
