"""
The browser paints the field with the same colours the server used to.

Moving the render into the client only works if a colour keeps meaning what
it meant: the legend, the stored contour images and anyone's memory of last
week's map all assume the same colormaps. The ramps in
dashboard/map_colours.py are sampled from map_interpolation.VARIABLE_CMAPS,
and this regenerates them to catch drift — the same guard the legend has,
for the same reason.
"""

from unittest import TestCase

import matplotlib

from dashboard.map_colours import COLOUR_RAMPS, RAMP_STEPS, colour_ramps
from forecasts.engine.map_interpolation import VARIABLE_CMAPS

TOLERANCE = 1


class ColourRampTests(TestCase):

    def test_every_rendered_variable_has_a_ramp(self):
        """Anything the map can draw must have colours to draw it with."""
        self.assertEqual(set(COLOUR_RAMPS), set(VARIABLE_CMAPS))

    def test_the_ramps_match_the_colormaps(self):
        for variable, spec in COLOUR_RAMPS.items():
            with self.subTest(variable=variable):
                cmap = matplotlib.colormaps[VARIABLE_CMAPS[variable]["cmap"]]

                self.assertEqual(len(spec["ramp"]), RAMP_STEPS)

                for i, got in enumerate(spec["ramp"]):
                    r, g, b, _ = cmap(i / (RAMP_STEPS - 1))
                    want = [round(r * 255), round(g * 255), round(b * 255)]
                    for a, b_ in zip(got, want):
                        self.assertLessEqual(
                            abs(a - b_), TOLERANCE,
                            f"{variable} stop {i}: {got} != {want}",
                        )

    def test_the_ranges_match_the_renderer(self):
        """
        vmin and vmax decide what a colour means. If these drifted from
        VARIABLE_CMAPS the field would be painted against a different scale
        than the legend describes.
        """
        for variable, spec in COLOUR_RAMPS.items():
            with self.subTest(variable=variable):
                self.assertEqual(spec["vmin"], VARIABLE_CMAPS[variable]["vmin"])
                self.assertEqual(spec["vmax"], VARIABLE_CMAPS[variable]["vmax"])

    def test_the_named_colormap_is_recorded(self):
        """So a reader can see which colormap a ramp came from."""
        for variable, spec in COLOUR_RAMPS.items():
            with self.subTest(variable=variable):
                self.assertEqual(spec["cmap"], VARIABLE_CMAPS[variable]["cmap"])

    def test_it_is_json_safe(self):
        import json

        data = json.loads(json.dumps(colour_ramps()))
        self.assertEqual(set(data), set(COLOUR_RAMPS))
