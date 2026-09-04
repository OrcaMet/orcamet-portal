"""
Clearing the contour cache must not delete anything else, or delete
anything without being asked.

The images are dead weight now that the map paints its own field, but they
are also irreversible: nothing writes them any more, so a mistaken delete
can only be undone by re-running risk_grid with --contour-vars. That makes
"dry run unless told otherwise" the important behaviour here, alongside the
obvious one of not cascading into the grid points the map actually needs.
"""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from forecasts.models import (
    CachedContourImage, UKRiskGridPoint, UKRiskGridRun,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class ClearContourCacheTests(TestCase):

    def setUp(self):
        today = timezone.localdate()
        self.hour = timezone.now().replace(minute=0, second=0, microsecond=0)

        self.old = UKRiskGridRun.objects.create(
            forecast_date=today - timedelta(days=1),
            status=UKRiskGridRun.Status.SUCCESS,
            generated_at=timezone.now() - timedelta(days=1),
        )
        self.latest = UKRiskGridRun.objects.create(
            forecast_date=today,
            status=UKRiskGridRun.Status.SUCCESS,
            generated_at=timezone.now(),
        )

        for run in (self.old, self.latest):
            for variable in ("pcancel", "wind"):
                CachedContourImage.objects.create(
                    run=run, timestamp=self.hour,
                    variable=variable, image_data=PNG,
                )
            UKRiskGridPoint.objects.create(
                run=run, latitude=55.0, longitude=-3.0, timestamp=self.hour,
                wind_speed=8.0, wind_gusts=12.0, precipitation=0.1,
                temperature=10.0, risk=20.0, p_cancel=15.0,
            )

    def _run(self, *args):
        out = StringIO()
        call_command("clear_contour_cache", *args, stdout=out)
        return out.getvalue()

    # --------------------------------------------------------
    # Nothing happens unless asked
    # --------------------------------------------------------

    def test_it_is_a_dry_run_by_default(self):
        output = self._run()

        self.assertEqual(CachedContourImage.objects.count(), 4)
        self.assertIn("Dry run", output)

    def test_the_dry_run_reports_what_it_would_remove(self):
        output = self._run()

        self.assertIn("Contour images to remove: 4", output)

    def test_delete_actually_deletes(self):
        self._run("--delete")

        self.assertEqual(CachedContourImage.objects.count(), 0)

    # --------------------------------------------------------
    # It must not take anything else with it
    # --------------------------------------------------------

    def test_the_grid_points_survive(self):
        """The map is painted from these. Losing them would empty it."""
        self._run("--delete")

        self.assertEqual(UKRiskGridPoint.objects.count(), 2)

    def test_the_runs_survive(self):
        self._run("--delete")

        self.assertEqual(UKRiskGridRun.objects.count(), 2)

    # --------------------------------------------------------
    # Keeping a way back
    # --------------------------------------------------------

    def test_keep_latest_holds_back_the_newest_run(self):
        self._run("--delete", "--keep-latest")

        remaining = CachedContourImage.objects.all()
        self.assertEqual(remaining.count(), 2)
        self.assertTrue(all(i.run_id == self.latest.pk for i in remaining))

    def test_keep_latest_reports_what_it_kept(self):
        output = self._run("--keep-latest")

        self.assertIn("Keeping 2 image(s)", output)

    def test_keep_latest_still_needs_delete_to_act(self):
        self._run("--keep-latest")

        self.assertEqual(CachedContourImage.objects.count(), 4)

    # --------------------------------------------------------
    # Nothing to do
    # --------------------------------------------------------

    def test_an_empty_cache_is_not_an_error(self):
        CachedContourImage.objects.all().delete()

        output = self._run("--delete")

        self.assertIn("No contour images", output)

    def test_it_survives_a_run_with_no_images(self):
        CachedContourImage.objects.filter(run=self.old).delete()

        output = self._run("--delete")

        self.assertEqual(CachedContourImage.objects.count(), 0)
        self.assertIn("2", output)
