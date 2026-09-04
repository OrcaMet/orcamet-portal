"""
The two cron jobs must not start together.

Both were on "0 */6 * * *", so the per-site forecast run and the UK grid run
began at the same instant and competed for the same Open-Meteo minutely
quota. risk_grid is the ~12 minute job and the one that loses that race: it
backs its pacing off, defers batches to the retry sweep, and in a live run
lost every batch north of 55.9N — which is how whole latitude bands went
missing from the map while the run still reported success.

The blueprint is not what schedules the live jobs (those are configured in
the Render dashboard), so this guards the intent and the documentation of
it, not the deployed state.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

BLUEPRINT = Path(settings.BASE_DIR) / "render.yaml"

MINUTE = re.compile(r'schedule:\s*"(\d+)\s')


class CronScheduleTests(TestCase):

    def setUp(self):
        self.source = BLUEPRINT.read_text(encoding="utf-8")
        self.schedules = re.findall(r'schedule:\s*"([^"]+)"', self.source)

    def test_the_blueprint_defines_two_crons(self):
        self.assertEqual(len(self.schedules), 2, self.schedules)

    def test_they_do_not_start_at_the_same_minute(self):
        minutes = [MINUTE.search(f'schedule: "{s} ').group(1)
                   for s in self.schedules]

        self.assertNotEqual(
            minutes[0], minutes[1],
            "both crons start on the same minute and will contend for the "
            "Open-Meteo quota",
        )

    def test_the_grid_run_is_the_one_offset(self):
        """
        The site run is short; the grid run is long and rate-limit sensitive,
        so it is the one given clear air after the other has finished.
        """
        block = self.source[self.source.find("orcamet-portal_risk_grid"):]
        schedule = re.search(r'schedule:\s*"([^"]+)"', block).group(1)

        self.assertTrue(
            schedule.startswith("30 "),
            f"risk_grid schedule is {schedule!r}, expected a 30-minute offset",
        )

    def test_both_still_run_six_hourly(self):
        for schedule in self.schedules:
            with self.subTest(schedule=schedule):
                self.assertIn("*/6 * * *", schedule)

    def test_the_drift_from_the_dashboard_is_documented(self):
        """
        Changing this file does not change the live jobs. That has to stay
        written down next to the schedule, or it will be assumed otherwise.
        """
        self.assertIn("dashboard", self.source.lower())
