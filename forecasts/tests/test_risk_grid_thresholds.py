"""
End-to-end check that risk_grid scores the grid with the admin-editable
thresholds rather than a hardcoded set.

Runs the real pipeline with only the HTTP layer replaced, so the wiring
from MapThresholds through to a stored UKRiskGridPoint.risk is exercised
for real.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from forecasts.models import MapThresholds, UKRiskGridPoint, UKRiskGridRun


def fake_batch(model_name, lats, lons, start_date, end_date):
    """
    One flat day of borderline weather for every requested point.

    Values sit between the default caution and cancel thresholds for wind,
    gust and precipitation, so the resulting risk is sensitive to a change
    in either bound — with weather far outside the ramp, tightening the
    thresholds would produce no visible difference and the test would pass
    even if the setting were ignored.
    """
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(24)]

    return [
        {
            "lat": lat,
            "lon": lon,
            "time": times,
            "wind_speed": [12.0] * 24,
            "wind_gusts": [17.0] * 24,
            "precipitation": [1.2] * 24,
            "temperature": [6.0] * 24,
        }
        for lat, lon in zip(lats, lons)
    ]


def run_grid():
    """Run the pipeline over a coarse grid and return the mean stored risk."""
    with patch(
        "forecasts.management.commands.risk_grid.fetch_batch", side_effect=fake_batch
    ), patch("forecasts.management.commands.risk_grid.BATCH_DELAY", 0), \
            patch("forecasts.management.commands.risk_grid.MODEL_DELAY", 0):
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
    return sum(p.risk for p in points) / points.count()


class RiskGridUsesEditableThresholdsTests(TestCase):
    def test_tightening_thresholds_raises_the_scored_risk(self):
        """
        The same weather must score higher once the limits are tightened.
        If risk_grid still read a hardcoded dict, both runs would match.
        """
        baseline = run_grid()

        tight = MapThresholds.load()
        tight.wind_mean_caution = 6.0
        tight.wind_mean_cancel = 9.0
        tight.gust_caution = 9.0
        tight.gust_cancel = 13.0
        tight.precip_caution = 0.2
        tight.precip_cancel = 0.8
        tight.full_clean()
        tight.save()

        tightened = run_grid()

        self.assertGreater(
            tightened, baseline,
            "tightening the admin thresholds did not change the scored risk — "
            "risk_grid is not reading MapThresholds",
        )

    def test_relaxing_thresholds_lowers_the_scored_risk(self):
        baseline = run_grid()

        relaxed = MapThresholds.load()
        relaxed.wind_mean_caution = 20.0
        relaxed.wind_mean_cancel = 30.0
        relaxed.gust_caution = 28.0
        relaxed.gust_cancel = 40.0
        relaxed.precip_caution = 5.0
        relaxed.precip_cancel = 12.0
        relaxed.full_clean()
        relaxed.save()

        self.assertLess(run_grid(), baseline)

    def test_run_creates_the_threshold_row_if_absent(self):
        """A fresh database must not need the admin visited first."""
        MapThresholds.objects.all().delete()

        run_grid()

        self.assertEqual(MapThresholds.objects.count(), 1)
