"""
End-to-end check that risk_grid reads the admin-editable thresholds, and
that the grid's headline layer is a chance of cancellation.

Runs the real pipeline with only the HTTP layer replaced, so the wiring from
MapThresholds through to a stored UKRiskGridPoint.p_cancel is exercised for
real.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from forecasts.models import MapThresholds, UKRiskGridPoint, UKRiskGridRun

# Members spanning a useful range: at the default 20 m/s gust cancel limit
# none of these breach, so tightening the limit is what moves the number.
MEMBER_GUSTS = [4.0, 8.0, 11.0, 13.0, 16.0]


def fake_grid_members(lats, lons, forecast_days=2, model=None):
    """One flat day per point, one member per value in MEMBER_GUSTS."""
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(24)]

    per_point = []
    for _ in lats:
        per_point.append([
            {
                "wind_speed_10m": [g * 0.6] * 24,
                "wind_gusts_10m": [g] * 24,
                "precipitation": [0.0] * 24,
                "temperature_2m": [12.0] * 24,
            }
            for g in MEMBER_GUSTS
        ])
    return times, per_point


def run_grid():
    """Run the pipeline over a coarse grid; return the mean stored p_cancel."""
    with patch(
        "forecasts.engine.ensemble.fetch_grid_members", side_effect=fake_grid_members
    ), patch("forecasts.management.commands.risk_grid.BATCH_DELAY", 0):
        call_command(
            "risk_grid",
            "--resolution", "4.0",
            "--days", "1",
            "--contour-vars", "none",
            verbosity=0,
        )

    run = UKRiskGridRun.objects.latest("id")
    points = UKRiskGridPoint.objects.filter(run=run)
    assert points.exists(), "pipeline stored no grid points"
    values = [p.p_cancel for p in points if p.p_cancel is not None]
    assert values, "no point carried a cancellation probability"
    return sum(values) / len(values)


class GridCancellationProbabilityTests(TestCase):
    def test_no_member_breaching_gives_zero(self):
        """Default limits sit above every member."""
        self.assertEqual(run_grid(), 0.0)

    def test_tightening_thresholds_raises_the_chance_of_cancellation(self):
        """
        If risk_grid still read a hardcoded threshold set, this would not move.
        Limit of 10 m/s puts 3 of the 5 members over.
        """
        th = MapThresholds.load()
        th.gust_caution = 8.0
        th.gust_cancel = 10.0
        th.wind_mean_caution = 20.0
        th.wind_mean_cancel = 30.0
        th.full_clean()
        th.save()

        self.assertAlmostEqual(run_grid(), 60.0)

    def test_relaxing_thresholds_lowers_it_again(self):
        th = MapThresholds.load()
        th.gust_caution = 8.0
        th.gust_cancel = 10.0
        th.full_clean()
        th.save()
        tightened = run_grid()

        th = MapThresholds.load()
        th.gust_caution = 30.0
        th.gust_cancel = 40.0
        th.full_clean()
        th.save()

        self.assertLess(run_grid(), tightened)

    def test_members_are_recorded_for_provenance(self):
        run_grid()
        point = UKRiskGridPoint.objects.exclude(ensemble_members=None).first()
        self.assertEqual(point.ensemble_members, len(MEMBER_GUSTS))

    def test_weather_layers_hold_the_member_mean(self):
        run_grid()
        point = UKRiskGridPoint.objects.first()
        self.assertAlmostEqual(
            point.wind_gusts, sum(MEMBER_GUSTS) / len(MEMBER_GUSTS), places=1
        )

    def test_run_creates_the_threshold_row_if_absent(self):
        """A fresh database must not need the admin visited first."""
        MapThresholds.objects.all().delete()
        run_grid()
        self.assertEqual(MapThresholds.objects.count(), 1)

    def test_run_records_the_ensemble_it_used(self):
        run_grid()
        run = UKRiskGridRun.objects.latest("id")
        self.assertEqual(run.models_used, ["ecmwf_ifs025_ensemble"])
