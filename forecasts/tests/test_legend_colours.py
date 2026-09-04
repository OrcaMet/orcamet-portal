"""
The legend must show the colours the contours are actually drawn in.

The map carried a hand-written table of RGB triples that had drifted from
the colormaps in map_interpolation.VARIABLE_CMAPS. The wind key showed green
at 7 m/s where YlOrRd is pale yellow, and claimed a dark slate zero where
the colormap is near-white — so the key described a map that was never
drawn, on a page whose whole purpose is reading colour as severity.

The table now lives in dashboard/map_legend.py as plain data, because
importing matplotlib into a gunicorn worker to draw five swatches would cost
tens of megabytes per worker. This test is what keeps that copy honest: it
regenerates the values from the colormaps and fails if they drift again.
"""

from unittest import TestCase

import matplotlib

from dashboard.map_legend import LEGENDS, STOP_COUNT, legend_data
from forecasts.engine.map_interpolation import VARIABLE_CMAPS

# Palette entries are stored rounded to whole channel values.
TOLERANCE = 1


def _expected(variable):
    spec = VARIABLE_CMAPS[variable]
    cmap = matplotlib.colormaps[spec["cmap"]]

    stops = []
    for i in range(STOP_COUNT):
        fraction = i / (STOP_COUNT - 1)
        value = spec["vmin"] + fraction * (spec["vmax"] - spec["vmin"])
        r, g, b, _ = cmap(fraction)
        stops.append((
            round(value, 2),
            [round(r * 255), round(g * 255), round(b * 255)],
        ))
    return stops


class LegendColourTests(TestCase):

    def test_every_map_variable_has_a_legend(self):
        """The map's five tabs; `risk` is not one of them any more."""
        self.assertEqual(
            set(LEGENDS), {"pcancel", "wind", "gust", "precip", "temp"},
        )

    def test_the_stored_colours_match_the_colormaps(self):
        for variable in LEGENDS:
            with self.subTest(variable=variable):
                expected = _expected(variable)
                stored = LEGENDS[variable]["stops"]

                self.assertEqual(len(stored), len(expected))

                for (got_value, got_rgb), (want_value, want_rgb) in zip(
                    stored, expected
                ):
                    self.assertAlmostEqual(got_value, want_value, places=2)
                    for got, want in zip(got_rgb, want_rgb):
                        self.assertLessEqual(
                            abs(got - want), TOLERANCE,
                            f"{variable}: {got_rgb} != {want_rgb}",
                        )

    def test_the_stops_span_the_full_colour_range(self):
        """A legend that stopped short would understate the top of the scale."""
        for variable, spec in LEGENDS.items():
            with self.subTest(variable=variable):
                cmap = VARIABLE_CMAPS[variable]
                self.assertAlmostEqual(spec["stops"][0][0], cmap["vmin"])
                self.assertAlmostEqual(spec["stops"][-1][0], cmap["vmax"])


class LegendLabelTests(TestCase):

    def setUp(self):
        self.data = legend_data()

    def test_it_keeps_the_shape_the_map_renders(self):
        entry = self.data["wind"]
        self.assertIn("t", entry)
        self.assertEqual(len(entry["s"][0]), 3)   # [value, rgb, label]

    def test_a_clamped_scale_reads_as_open_ended(self):
        """The contour clamps above vmax, so the top band means 'or more'."""
        self.assertEqual(self.data["wind"]["s"][-1][2], "25+")
        self.assertEqual(self.data["gust"]["s"][-1][2], "35+")

    def test_a_probability_is_not_open_ended(self):
        """Nothing is more likely than certain."""
        self.assertEqual(self.data["pcancel"]["s"][-1][2], "100%")

    def test_negative_values_survive(self):
        self.assertEqual(self.data["temp"]["s"][0][2], "-5")
