"""
The grid run must report the coverage it achieved, not the coverage it asked
for.

grid_points is what the map prints as its point count, and it used to be
written once at run creation from the intended grid size and never revisited.
A run that lost points — to rate limiting, a refused key, a batch error —
still claimed the full grid, so the map overstated its own coverage by more
than half on a bad morning while showing an obvious hole over Scotland.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
from django.core.management import call_command
from django.test import TestCase

from forecasts.models import UKRiskGridPoint, UKRiskGridRun

# Coarse enough to keep the run quick; 4 lats x 4 lons over the UK box.
RESOLUTION = "4.0"

MEMBER_GUSTS = [6.0, 9.0, 12.0]

# Points on this meridian come back with no members, standing in for whatever
# lost them upstream. Never the first grid point — that one is the probe, and
# an empty probe aborts the run before there is anything to count.
DEAD_LON = 0.4


def _members():
    return [
        {
            "wind_speed_10m": [g * 0.6] * 24,
            "wind_gusts_10m": [g] * 24,
            "precipitation": [0.0] * 24,
            "temperature_2m": [11.0] * 24,
        }
        for g in MEMBER_GUSTS
    ]


def _times():
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return [
        (start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M")
        for h in range(24)
    ]


def full_coverage(lats, lons, forecast_days=2, model=None):
    return _times(), [_members() for _ in lats]


def partial_coverage(lats, lons, forecast_days=2, model=None):
    """Empty member list for the dead meridian — the API's way of returning
    nothing useful for a point without failing the whole batch."""
    return _times(), [
        [] if round(lon, 4) == DEAD_LON else _members()
        for lon in lons
    ]


def run_grid(fetch):
    with patch(
        "forecasts.engine.ensemble.fetch_grid_members", side_effect=fetch
    ), patch("forecasts.management.commands.risk_grid.BATCH_DELAY", 0):
        call_command(
            "risk_grid",
            "--resolution", RESOLUTION,
            "--days", "1",
            "--contour-vars", "none",
            verbosity=0,
        )
    return UKRiskGridRun.objects.latest("id")


def stored_points(run):
    """Distinct coordinates held for the run — one point, many hours."""
    return (
        UKRiskGridPoint.objects.filter(run=run)
        .values("latitude", "longitude")
        .distinct()
        .count()
    )


class GridCoverageReportingTests(TestCase):
    def test_full_run_reports_every_point(self):
        run = run_grid(full_coverage)
        self.assertEqual(run.status, UKRiskGridRun.Status.SUCCESS)
        self.assertEqual(run.grid_points, stored_points(run))

    def test_partial_run_reports_only_what_it_holds(self):
        """The regression itself: dropped points must not be counted."""
        run = run_grid(partial_coverage)
        self.assertEqual(run.status, UKRiskGridRun.Status.SUCCESS)

        held = stored_points(run)
        self.assertEqual(run.grid_points, held)

        # And the run really did lose points, or the assertion above would
        # pass for the wrong reason.
        dead = UKRiskGridPoint.objects.filter(
            run=run, longitude=DEAD_LON
        ).count()
        self.assertEqual(dead, 0, "the dead meridian still stored points")

    def test_intended_grid_size_stays_recoverable(self):
        """Overwriting grid_points is only safe because the size asked for can
        still be worked out from the run's own bounds."""
        run = run_grid(partial_coverage)

        # The same construction risk_grid uses to lay the grid out.
        lats = np.arange(run.lat_min, run.lat_max + run.resolution, run.resolution)
        lons = np.arange(run.lon_min, run.lon_max + run.resolution, run.resolution)
        intended = len(lats) * len(lons)

        self.assertGreater(intended, run.grid_points)
