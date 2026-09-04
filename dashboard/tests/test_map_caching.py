"""
OrcaMet Portal — Caching of the map frame endpoints.

The contour PNGs and the grid point arrays were served with no caching
headers at all. Every frame of a 72-hour playback was therefore a fresh
database round trip pulling a BLOB through a gunicorn worker, repeated for
every viewer, and again each time the variable tabs were switched.

The content is immutable when it is fully addressed: a grid run's rows and
images are written once, and a new run gets a new key. That is only true
when the client names both the run and the hour, though — a frame reached by
falling back to "the latest run" or "the first hour" moves under the client,
and must not be cached long. These tests pin that distinction, since getting
it wrong in the permissive direction would freeze a stale run on screen.
"""

from datetime import timedelta
from urllib.parse import quote

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from forecasts.models import (
    CachedContourImage, UKRiskGridPoint, UKRiskGridRun,
)

# A one-pixel transparent PNG. The endpoint serves bytes it does not inspect.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


class FrameCachingTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username="steve", password="x", role=User.Role.SUPERADMIN,
        )
        self.client.force_login(self.user)

        self.hour = timezone.now().replace(minute=0, second=0, microsecond=0)

        self.run = UKRiskGridRun.objects.create(
            forecast_date=timezone.localdate(),
            status=UKRiskGridRun.Status.SUCCESS,
            grid_points=4,
            num_hours=2,
        )
        for offset in (0, 1):
            CachedContourImage.objects.create(
                run=self.run,
                timestamp=self.hour + timedelta(hours=offset),
                variable="pcancel",
                image_data=PNG,
            )
            for lat, lon in ((51.5, -0.1), (55.0, -3.0)):
                UKRiskGridPoint.objects.create(
                    run=self.run,
                    latitude=lat, longitude=lon,
                    timestamp=self.hour + timedelta(hours=offset),
                    wind_speed=5.0, wind_gusts=9.0,
                    precipitation=0.1, temperature=11.0,
                    risk=12.0, p_cancel=4.0,
                )

        self.contour_url = reverse("dashboard:map_contour_image")
        self.points_url = reverse("dashboard:map_grid_points_json")
        self.ts_url = reverse("dashboard:map_contour_timestamps")

    def _stamp(self, hour):
        # The offset's "+" must be percent-encoded or the query parser reads
        # it as a space, which is how the real client sends it
        # (encodeURIComponent) and how these tests must send it too.
        return quote(hour.isoformat(), safe="")

    def _addressed(self, url, hour=None):
        return "%s?run=%d&timestamp=%s" % (
            url, self.run.pk, self._stamp(hour or self.hour),
        )

    # --------------------------------------------------------
    # Fully addressed frames are immutable
    # --------------------------------------------------------

    def test_an_addressed_contour_frame_is_immutable(self):
        r = self.client.get(self._addressed(self.contour_url) + "&var=pcancel")

        self.assertEqual(r.status_code, 200)
        self.assertIn("immutable", r["Cache-Control"])
        self.assertIn("max-age=", r["Cache-Control"])
        self.assertTrue(r["ETag"])

    def test_an_addressed_points_frame_is_immutable(self):
        r = self.client.get(self._addressed(self.points_url))

        self.assertEqual(r.status_code, 200)
        self.assertIn("immutable", r["Cache-Control"])
        self.assertTrue(r["ETag"])

    def test_frames_are_never_cached_publicly(self):
        """They come from a login-gated URL; no shared proxy should hold them."""
        for url in (self._addressed(self.contour_url) + "&var=pcancel",
                    self._addressed(self.points_url)):
            with self.subTest(url=url):
                self.assertIn("private", self.client.get(url)["Cache-Control"])

    # --------------------------------------------------------
    # Fallbacks are not
    # --------------------------------------------------------

    def test_a_contour_without_a_run_key_is_not_immutable(self):
        """'The latest run' changes under the client."""
        r = self.client.get(
            "%s?var=pcancel&timestamp=%s" % (self.contour_url, self._stamp(self.hour))
        )

        self.assertEqual(r.status_code, 200)
        self.assertNotIn("immutable", r["Cache-Control"])

    def test_a_contour_without_a_timestamp_is_not_immutable(self):
        """Falling back to the first hour is a moving target too."""
        r = self.client.get(
            "%s?var=pcancel&run=%d" % (self.contour_url, self.run.pk)
        )

        self.assertEqual(r.status_code, 200)
        self.assertNotIn("immutable", r["Cache-Control"])

    def test_points_without_a_run_key_are_not_immutable(self):
        r = self.client.get(
            "%s?timestamp=%s" % (self.points_url, self._stamp(self.hour))
        )

        self.assertEqual(r.status_code, 200)
        self.assertNotIn("immutable", r["Cache-Control"])

    # --------------------------------------------------------
    # Revalidation
    # --------------------------------------------------------

    def test_a_matching_etag_gets_a_304(self):
        url = self._addressed(self.contour_url) + "&var=pcancel"
        first = self.client.get(url)

        second = self.client.get(url, HTTP_IF_NONE_MATCH=first["ETag"])

        self.assertEqual(second.status_code, 304)
        self.assertEqual(second["ETag"], first["ETag"])

    def test_a_304_carries_no_body(self):
        """The whole point is not sending the BLOB again."""
        url = self._addressed(self.contour_url) + "&var=pcancel"
        etag = self.client.get(url)["ETag"]

        second = self.client.get(url, HTTP_IF_NONE_MATCH=etag)

        self.assertEqual(second.content, b"")

    def test_points_revalidate_too(self):
        url = self._addressed(self.points_url)
        first = self.client.get(url)

        second = self.client.get(url, HTTP_IF_NONE_MATCH=first["ETag"])

        self.assertEqual(second.status_code, 304)

    def test_a_stale_etag_gets_the_frame(self):
        url = self._addressed(self.contour_url) + "&var=pcancel"

        r = self.client.get(url, HTTP_IF_NONE_MATCH='"something-else"')

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, PNG)

    def test_different_hours_have_different_etags(self):
        """An ETag shared across hours would serve one frame for all of them."""
        base = "%s?run=%d&var=pcancel&timestamp=" % (self.contour_url, self.run.pk)

        first = self.client.get(base + self._stamp(self.hour))["ETag"]
        second = self.client.get(
            base + self._stamp(self.hour + timedelta(hours=1))
        )["ETag"]

        self.assertNotEqual(first, second)

    def test_different_variables_have_different_etags(self):
        CachedContourImage.objects.create(
            run=self.run, timestamp=self.hour, variable="wind", image_data=PNG,
        )
        base = self._addressed(self.contour_url) + "&var="

        self.assertNotEqual(
            self.client.get(base + "pcancel")["ETag"],
            self.client.get(base + "wind")["ETag"],
        )

    # --------------------------------------------------------
    # The discovery document must not be cached
    # --------------------------------------------------------

    def test_the_timestamps_endpoint_is_not_cached(self):
        """
        It hands out the run key and is how a long-open map notices a new
        grid run. Caching it would pin the session to a stale run.
        """
        r = self.client.get(self.ts_url)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Cache-Control"], "no-store")

    def test_the_empty_timestamps_response_is_not_cached_either(self):
        UKRiskGridRun.objects.all().delete()

        r = self.client.get(self.ts_url)

        self.assertEqual(r["Cache-Control"], "no-store")
