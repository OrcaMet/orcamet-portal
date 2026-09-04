"""
OrcaMet Portal — Tests for the map timeline and boot path fixes.

Pins three defects found auditing the weather map:

1. fitBounds only ran on the boot success path, so a slow or failed contour
   load left the map on the default UK-wide view.
2. The 5-minute auto-refresh fetched new data but never re-rendered it.
3. The hourly frames only covered one ForecastRun (a single day), so past
   the first 24 hours the site markers silently fell back to a peak-of-day
   summary while the contour kept advancing hour by hour.
"""

import json
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db.models.signals import post_save
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from forecasts.models import ForecastRun, HourlyForecast
from sites.models import Client as SiteClient, Site
from sites.signals import trigger_forecast_on_site_save

TEMPLATE = (
    Path(settings.BASE_DIR)
    / "dashboard" / "templates" / "dashboard" / "weather_map.html"
)


def _make_run(site, day, hours=24, start_hour=0, generated_offset=0):
    """
    Create a successful run for `day` with `hours` hourly rows.

    generated_offset separates runs for the same date: ForecastRun is unique
    on (site, forecast_date, generated_at), and two creates in the same tick
    would otherwise collide.
    """
    run = ForecastRun.objects.create(
        site=site,
        forecast_date=day,
        status=ForecastRun.Status.SUCCESS,
        generated_at=timezone.now() + timedelta(seconds=generated_offset),
        peak_risk=10.0,
        recommendation="GO",
    )
    base = datetime(
        day.year, day.month, day.day, start_hour, tzinfo=dt_timezone.utc
    )
    HourlyForecast.objects.bulk_create([
        HourlyForecast(
            run=run,
            timestamp=base + timedelta(hours=i),
            wind_speed=5.0, wind_gusts=9.0,
            precipitation=0.0, temperature=12.0,
            hourly_risk=10.0,
        )
        for i in range(hours)
    ])
    return run


class HourlyTimelineHorizonTests(TestCase):
    """The hourly frames must span the whole forecast horizon."""

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
        self.org = SiteClient.objects.create(name="Acme Rope Access")
        self.site = Site.objects.create(
            client=self.org, name="Tower", postcode="BS1 4DJ",
            latitude=51.45, longitude=-2.58,
        )
        self.client.force_login(self.user)
        self.today = timezone.localdate()

    def _frames(self):
        resp = self.client.get(reverse("dashboard:map_sites_hourly_json"))
        self.assertEqual(resp.status_code, 200)
        return json.loads(resp.content)

    def test_frames_span_every_forecast_day_not_just_the_first(self):
        """
        Three days of runs must yield 72 hourly frames.

        Previously only the newest run was used, so this returned 24 and the
        markers stopped tracking the timeline after the first day.
        """
        for offset in range(3):
            _make_run(self.site, self.today + timedelta(days=offset))

        data = self._frames()

        self.assertEqual(len(data["timestamps"]), 72)

    def test_every_frame_carries_the_site(self):
        for offset in range(3):
            _make_run(self.site, self.today + timedelta(days=offset))

        data = self._frames()

        for ts in data["timestamps"]:
            self.assertEqual(len(data["frames"][ts]["features"]), 1, ts)

    def test_timestamps_are_sorted_across_run_boundaries(self):
        """Merging several runs must not interleave them out of order."""
        for offset in reversed(range(3)):  # create newest first
            _make_run(self.site, self.today + timedelta(days=offset))

        data = self._frames()

        self.assertEqual(data["timestamps"], sorted(data["timestamps"]))

    def test_runs_beyond_the_horizon_are_excluded(self):
        _make_run(self.site, self.today)
        _make_run(self.site, self.today + timedelta(days=30))

        data = self._frames()

        self.assertEqual(len(data["timestamps"]), 24)

    def test_past_runs_are_excluded(self):
        _make_run(self.site, self.today - timedelta(days=1))
        _make_run(self.site, self.today)

        data = self._frames()

        self.assertEqual(len(data["timestamps"]), 24)

    def test_only_the_latest_run_per_day_is_used(self):
        """A re-run for the same date must replace, not duplicate."""
        _make_run(self.site, self.today, generated_offset=0)
        _make_run(self.site, self.today, generated_offset=60)  # newer, same date

        data = self._frames()

        self.assertEqual(len(data["timestamps"]), 24)
        for ts in data["timestamps"]:
            self.assertEqual(len(data["frames"][ts]["features"]), 1)

    def test_failed_runs_are_ignored(self):
        _make_run(self.site, self.today)
        bad = ForecastRun.objects.create(
            site=self.site,
            forecast_date=self.today + timedelta(days=1),
            status=ForecastRun.Status.FAILED,
        )
        HourlyForecast.objects.create(
            run=bad,
            timestamp=timezone.now() + timedelta(days=1),
            wind_speed=99.0, wind_gusts=99.0,
            precipitation=9.0, temperature=0.0, hourly_risk=99.0,
        )

        data = self._frames()

        self.assertEqual(len(data["timestamps"]), 24)


class BootPathTemplateTests(TestCase):
    """
    Guard the JavaScript boot path.

    These assert on the template source rather than executing it — there is
    no JS runtime in the test suite — so they are deliberately narrow: they
    only check that each boot path still zooms to the sites, and that the
    refresh still re-renders. They exist because both defects were silent.
    """

    def setUp(self):
        self.source = TEMPLATE.read_text(encoding="utf-8")

    def test_fit_to_sites_helper_exists(self):
        self.assertIn("function fitToSites()", self.source)

    def test_every_boot_path_fits_to_sites(self):
        """Success path, safety timeout and error handler must all zoom."""
        self.assertEqual(self.source.count("fitToSites();"), 3)

    def test_no_inline_fitbounds_left_behind(self):
        """fitBounds should only be reachable through the helper."""
        self.assertEqual(self.source.count("map.fitBounds("), 1)

    def test_periodic_refresh_rerenders(self):
        """Refetching without re-rendering left stale pins on screen."""
        idx = self.source.find("}, 300000);")
        self.assertNotEqual(idx, -1, "5-minute refresh interval not found")

        block = self.source[max(0, idx - 400):idx]
        self.assertIn("loadSites()", block)
        self.assertIn("loadHourly()", block)
        self.assertIn("renderSites(", block)


class SiteFrameLookupTests(TestCase):
    """
    Site frames must be found by instant, not by timeline position.

    The contour timeline and the site frames come from two separate crons.
    The site runner drops hours with incomplete ensemble data and skips a
    whole day whose work window has none, and either cron can fail on its
    own — so the two axes can differ in length and in where they start.
    Indexing one by the other's position handed every pin some other hour's
    weather, with the clock reading the grid's hour and nothing on screen
    saying the two had diverged.

    As with BootPathTemplateTests, these assert on the template source: the
    suite has no JS runtime, so they are deliberately narrow.
    """

    def setUp(self):
        self.source = TEMPLATE.read_text(encoding="utf-8")

    def test_frames_are_indexed_by_instant(self):
        self.assertIn("function timeKey(", self.source)
        self.assertIn("function indexHourly(", self.source)
        self.assertIn("Date.parse(ts)", self.source)

    def test_the_index_is_rebuilt_whenever_hourly_data_arrives(self):
        """A refresh that did not reindex would match against stale hours."""
        idx = self.source.find("function loadHourly()")
        self.assertNotEqual(idx, -1, "loadHourly not found")
        self.assertIn("indexHourly()", self.source[idx:idx + 400])

    def test_render_sites_takes_a_timestamp_not_an_index(self):
        self.assertIn("function renderSites(ts)", self.source)

    def test_no_call_site_passes_a_position(self):
        """
        renderSites(curIdx) and renderSites(0) were the defect. Every caller
        must now pass an instant, or null where there is no timeline.
        """
        self.assertNotIn("renderSites(curIdx)", self.source)
        self.assertNotIn("renderSites(0)", self.source)

    def test_a_missing_hour_is_not_borrowed_from_another(self):
        """An uncovered hour must read as unknown, not as someone else's."""
        self.assertIn("no site forecast for this hour", self.source)
        self.assertIn("function popNoHour(", self.source)
        self.assertIn("addUnknownMarkers(", self.source)

    def test_the_gap_is_stated_on_screen(self):
        """The note has to reach the user, not just the marker icons."""
        idx = self.source.find("$bsub.textContent = 'Hour '")
        self.assertNotEqual(idx, -1, "the time bar no longer states the hour")

        # The note is assembled just above that assignment and interpolated
        # into it. Grid coverage now shares the same slot, so match the
        # assembly rather than one particular ternary.
        block = self.source[max(0, idx - 800):idx + 200]
        self.assertIn("siteHourNote", block)
        self.assertIn("note ? ", block)

    def test_the_summary_fallback_survives(self):
        """With no timeline at all, the daily summary is still shown."""
        self.assertIn("renderSites(null)", self.source)
        self.assertIn("function addSummaryMarkers(", self.source)
