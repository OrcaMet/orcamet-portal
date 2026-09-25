"""
A risk_grid run that crashes part-way is marked FAILED, not left RUNNING.

handle() only learned of the run through _run_pipeline's return value, which
never arrives when the pipeline raises — so its "mark as failed" branch saw
None and left the run marked RUNNING for good.
"""

from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from forecasts.models import UKRiskGridRun
from forecasts.tests.test_rate_limit_recovery import _members


def _api(lats, lons, forecast_days=2, model=None):
    return _members(lats)


def _run(*extra):
    with patch("forecasts.engine.ensemble.fetch_grid_members", side_effect=_api), \
         patch("forecasts.management.commands.risk_grid.time.sleep"):
        call_command(
            "risk_grid", "--resolution", "4.0", "--days", "1",
            *extra, verbosity=0,
        )


class GridCrashStatusTests(TestCase):
    def test_a_crash_mid_pipeline_marks_the_run_failed(self):
        with patch(
            "forecasts.management.commands.risk_grid.calculate_hourly_risk",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(CommandError):
                _run("--contour-vars", "none")

        run = UKRiskGridRun.objects.get()
        self.assertEqual(run.status, UKRiskGridRun.Status.FAILED)
        self.assertIn("boom", run.error_message)

    def test_a_crash_after_success_leaves_the_run_successful(self):
        """Contour rendering runs after SUCCESS; the grid data is complete."""
        with patch(
            "forecasts.management.commands.risk_grid.Command._render_contours",
            side_effect=RuntimeError("render failed"),
        ):
            with self.assertRaises(CommandError):
                _run("--contour-vars", "risk")

        run = UKRiskGridRun.objects.get()
        self.assertEqual(run.status, UKRiskGridRun.Status.SUCCESS)

    def test_a_clean_run_is_unaffected(self):
        _run("--contour-vars", "none")

        self.assertEqual(
            UKRiskGridRun.objects.get().status, UKRiskGridRun.Status.SUCCESS
        )
