"""
OrcaMet Portal — Tests for contour overlay caching.

CachedContourImage was read by three views but written by nothing, so
/dashboard/map/contour.png always 404'd and the map never showed a contour
layer. risk_grid now pre-renders the overlays; these tests cover that path.
"""

from datetime import datetime, timezone

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from forecasts.management.commands.risk_grid import Command as RiskGridCommand
from forecasts.models import (
    CachedContourImage,
    UKRiskGridPoint,
    UKRiskGridRun,
)


def _build_grid_run(n_side=4, hours=2):
    """Create a small but valid grid run (needs >= 4 points to interpolate)."""
    run = UKRiskGridRun.objects.create(
        forecast_date=datetime(2026, 8, 9).date(),
        status=UKRiskGridRun.Status.SUCCESS,
        resolution=0.5,
        grid_points=n_side * n_side,
        num_hours=hours,
        models_used=["ecmwf"],
    )

    points = []
    for h in range(hours):
        ts = datetime(2026, 8, 9, 9 + h, tzinfo=timezone.utc)
        for i in range(n_side):
            for j in range(n_side):
                lat = 50.0 + i * 2.0
                lon = -6.0 + j * 2.0
                points.append(UKRiskGridPoint(
                    run=run, latitude=lat, longitude=lon, timestamp=ts,
                    wind_speed=5.0 + i, wind_gusts=10.0 + j,
                    precipitation=0.1 * j, temperature=8.0 + i,
                    risk=10.0 * (i + j),
                ))
    UKRiskGridPoint.objects.bulk_create(points)
    return run


class ContourCacheTests(TestCase):

    def test_render_contours_populates_cached_images(self):
        run = _build_grid_run()
        self.assertEqual(CachedContourImage.objects.count(), 0)

        RiskGridCommand()._render_contours(run, ["risk"])

        images = CachedContourImage.objects.filter(run=run, variable="risk")
        self.assertEqual(images.count(), 2)  # one per timestamp

        for image in images:
            data = bytes(image.image_data)
            self.assertGreater(len(data), 0)
            # PNG magic number
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")

    def test_render_contours_supports_multiple_variables(self):
        run = _build_grid_run(hours=1)

        RiskGridCommand()._render_contours(run, ["risk", "wind", "temp"])

        self.assertEqual(
            set(
                CachedContourImage.objects
                .filter(run=run)
                .values_list("variable", flat=True)
            ),
            {"risk", "wind", "temp"},
        )

    def test_rerunning_does_not_violate_unique_constraint(self):
        run = _build_grid_run(hours=1)

        RiskGridCommand()._render_contours(run, ["risk"])
        RiskGridCommand()._render_contours(run, ["risk"])

        self.assertEqual(
            CachedContourImage.objects.filter(run=run, variable="risk").count(), 1
        )

    def test_cached_image_is_served_by_the_contour_endpoint(self):
        from django.urls import reverse
        from accounts.models import User

        run = _build_grid_run(hours=1)
        RiskGridCommand()._render_contours(run, ["risk"])

        user = User.objects.create_user(
            username="steve", password="x", role=User.Role.SUPERADMIN,
        )
        self.client.force_login(user)

        resp = self.client.get(reverse("dashboard:map_contour_image"), {"var": "risk"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(resp.content[:8], b"\x89PNG\r\n\x1a\n")

    def test_too_few_points_is_skipped_not_fatal(self):
        run = UKRiskGridRun.objects.create(
            forecast_date=datetime(2026, 8, 9).date(),
            status=UKRiskGridRun.Status.SUCCESS,
            resolution=0.5, grid_points=2, num_hours=1,
        )
        ts = datetime(2026, 8, 9, 9, tzinfo=timezone.utc)
        UKRiskGridPoint.objects.bulk_create([
            UKRiskGridPoint(run=run, latitude=51.0, longitude=-1.0, timestamp=ts,
                            wind_speed=5.0, wind_gusts=9.0, precipitation=0.0,
                            temperature=8.0, risk=10.0),
            UKRiskGridPoint(run=run, latitude=52.0, longitude=-2.0, timestamp=ts,
                            wind_speed=5.0, wind_gusts=9.0, precipitation=0.0,
                            temperature=8.0, risk=20.0),
        ])

        RiskGridCommand()._render_contours(run, ["risk"])

        self.assertEqual(CachedContourImage.objects.filter(run=run).count(), 0)


class RiskGridArgumentTests(TestCase):

    def test_invalid_contour_var_is_rejected_before_any_api_call(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("risk_grid", "--contour-vars", "bogus")

        self.assertIn("Unknown --contour-vars", str(ctx.exception))

    def test_none_disables_contour_rendering(self):
        """'none' must parse to an empty list rather than a variable named 'none'."""
        command = RiskGridCommand()
        parser = command.create_parser("manage.py", "risk_grid")
        options = parser.parse_args(["--contour-vars", "none"])

        self.assertEqual(options.contour_vars, "none")
