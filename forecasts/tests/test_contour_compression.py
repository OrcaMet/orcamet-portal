"""
Contour frames are stored as palette PNGs, not 24-bit RGBA.

Every frame lives in the database as a BinaryField — five variables per
forecast hour, several runs live at once — so frame size is the dominant
term in what the grid costs to keep. A filled contour is a handful of flat
bands, and paying 32 bits a pixel to store 51 of them was most of that cost.

Quantising must not cost the two things the overlay depends on: the blanked
voids staying transparent (a gap must never read as measured data) and the
colours staying close enough that a band still means what the legend says.
"""

import io
from unittest import TestCase

import numpy as np
from PIL import Image

import forecasts.engine.map_interpolation as mi
from forecasts.engine.map_interpolation import (
    UK_LAT_MAX,
    UK_LAT_MIN,
    UK_LON_MAX,
    UK_LON_MIN,
    _to_palette_png,
    render_contour_to_bytes,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _smooth_field(spacing=0.5):
    """A plausible weather field — smooth, not noise."""
    lats, lons = np.meshgrid(
        np.arange(UK_LAT_MIN, UK_LAT_MAX + spacing, spacing),
        np.arange(UK_LON_MIN, UK_LON_MAX + spacing, spacing),
    )
    lats, lons = lats.ravel(), lons.ravel()
    values = 50 + 40 * np.sin(lats / 2.2) * np.cos(lons / 1.6)
    return lats, lons, np.clip(values, 0, 100)


def _render_both(variable="pcancel"):
    """
    Render once, returning (full colour bytes, stored bytes).

    The renderer compresses on its way out, so the full-colour original is
    captured by intercepting that last step rather than by rendering twice.
    """
    original = mi._to_palette_png
    captured = []

    def spy(data):
        captured.append(data)
        return original(data)

    mi._to_palette_png = spy
    try:
        stored = render_contour_to_bytes(*_smooth_field(), variable=variable)
    finally:
        mi._to_palette_png = original

    return captured[0], stored


class PaletteEncodingTests(TestCase):

    @classmethod
    def setUpClass(cls):
        cls.raw, cls.png = _render_both()

    def test_it_is_still_a_png(self):
        self.assertTrue(self.png.startswith(PNG_MAGIC))

    def test_the_stored_frame_is_a_palette_image(self):
        with Image.open(io.BytesIO(self.png)) as im:
            self.assertEqual(im.mode, "P")

    def test_the_overlay_is_semi_transparent_throughout(self):
        """
        The overlay is drawn over a basemap, so it is translucent everywhere
        it is drawn at all. A palette that dropped alpha would render it as
        an opaque sheet hiding the coastline underneath.
        """
        with Image.open(io.BytesIO(self.png)) as im:
            alpha = np.asarray(im.convert("RGBA"))[:, :, 3]

        self.assertTrue((alpha > 0).any(), "the whole frame went transparent")
        self.assertTrue((alpha < 255).all(), "the overlay became opaque")

    def test_blanked_voids_stay_transparent(self):
        """
        A gap in the grid is rendered as nothing at all, so it reads as "no
        data" rather than as weather. A palette built from RGB alone would
        flatten alpha to on/off and could lose that.
        """
        lats, lons, values = _smooth_field()

        # Remove a band, the way a rate-limited run loses whole latitudes.
        keep = (lats < 55.0) | (lats > 57.5)
        png = render_contour_to_bytes(
            lats[keep], lons[keep], values[keep], variable="pcancel"
        )

        with Image.open(io.BytesIO(png)) as im:
            alpha = np.asarray(im.convert("RGBA"))[:, :, 3]

        self.assertTrue((alpha == 0).any(), "the void was not left transparent")
        self.assertTrue((alpha > 0).any(), "the whole frame went transparent")

    def test_it_is_substantially_smaller(self):
        """The reason the change exists. Measured at ~21% across variables."""
        self.assertLess(len(self.png), 0.5 * len(self.raw))

    def test_colours_stay_close_to_the_full_colour_render(self):
        """A band must still read as the colour the legend gives it."""
        with Image.open(io.BytesIO(self.png)) as im:
            quantised = np.asarray(im.convert("RGBA"), dtype=int)
        with Image.open(io.BytesIO(self.raw)) as im:
            original = np.asarray(im.convert("RGBA"), dtype=int)

        self.assertEqual(original.shape, quantised.shape)

        diff = np.abs(original - quantised)
        self.assertLess(diff.max(), 40)
        self.assertLess(diff.mean(), 5)


class PaletteFallbackTests(TestCase):
    """Compression is an optimisation; it must never lose the frame."""

    def test_unreadable_bytes_come_back_unchanged(self):
        junk = b"not a png at all"
        self.assertEqual(_to_palette_png(junk), junk)

    def test_a_result_that_grew_is_discarded(self):
        """A frame must never be made bigger by trying to shrink it."""
        tiny = Image.new("RGBA", (2, 2), (255, 0, 0, 128))
        buf = io.BytesIO()
        tiny.save(buf, format="PNG", optimize=True)
        source = buf.getvalue()

        self.assertLessEqual(len(_to_palette_png(source)), len(source))

    def test_every_variable_still_renders(self):
        for variable in ("pcancel", "risk", "wind", "gust", "precip", "temp"):
            with self.subTest(variable=variable):
                png = render_contour_to_bytes(*_smooth_field(), variable=variable)
                self.assertTrue(png.startswith(PNG_MAGIC))
