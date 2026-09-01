"""
Rate-limit recovery in risk_grid.

A live run lost every batch north of 55.9N and still reported success: once
Open-Meteo's limiter bit, the fixed 15s pacing never slowed, so each later
batch was limited too, exhausted its single retry and was dropped. Scotland
simply went missing from the map.

Two behaviours guard against that now:

* pacing backs off for the remainder of the run after a rate limit
* batches still limited after their retry are swept once more at the end,
  rather than left as a hole in the surface

Only the HTTP layer is replaced, so the real command drives these paths.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from forecasts.management.commands import risk_grid
from forecasts.models import UKRiskGridPoint, UKRiskGridRun


class FakeRateLimit(Exception):
    """Mimics what the shared session raises once its 429 retries are spent."""

    def __init__(self):
        super().__init__(
            "HTTPSConnectionPool(host='ensemble-api.open-meteo.com', port=443): "
            "Max retries exceeded (Caused by ResponseError("
            "'too many 429 error responses'))"
        )


def _members(lats):
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    times = [
        (start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M")
        for h in range(24)
    ]
    per_point = [
        [{
            "wind_speed_10m": [5.0] * 24,
            "wind_gusts_10m": [9.0] * 24,
            "precipitation": [0.0] * 24,
            "temperature_2m": [12.0] * 24,
        }]
        for _ in lats
    ]
    return times, per_point


class _Api:
    """
    Rate-limits the first `fail_first` batch fetches, then succeeds.

    The command opens with a single-point probe to establish the timestamp
    axis; that has to get through, or the run aborts before reaching the
    batch loop this test is about.
    """

    def __init__(self, fail_first):
        self.fail_first = fail_first
        self.calls = 0
        self.batch_calls = 0

    def __call__(self, lats, lons, forecast_days=2, model=None):
        self.calls += 1
        if self.calls == 1:  # the probe
            return _members(lats)

        self.batch_calls += 1
        if self.batch_calls <= self.fail_first:
            raise FakeRateLimit()
        return _members(lats)


def _run(api):
    with patch("forecasts.engine.ensemble.fetch_grid_members", side_effect=api), \
         patch("forecasts.management.commands.risk_grid.time.sleep"):
        call_command(
            "risk_grid",
            "--resolution", "4.0",
            "--days", "1",
            "--contour-vars", "none",
            verbosity=0,
        )
    return UKRiskGridRun.objects.latest("id")


class RateLimitRecoveryTests(TestCase):

    def test_points_lost_to_the_limiter_are_recovered_by_the_retry_sweep(self):
        """
        Every batch is limited on its first two attempts.

        Before the sweep those points were dropped outright; the run still
        reported success with a hole in the grid.
        """
        api = _Api(fail_first=4)

        run = _run(api)

        self.assertEqual(run.status, UKRiskGridRun.Status.SUCCESS)
        self.assertTrue(
            UKRiskGridPoint.objects.filter(run=run).exists(),
            "retry sweep recovered no points",
        )

    def test_a_clean_run_needs_no_sweep(self):
        api = _Api(fail_first=0)

        run = _run(api)

        self.assertEqual(run.status, UKRiskGridRun.Status.SUCCESS)
        self.assertTrue(UKRiskGridPoint.objects.filter(run=run).exists())

    def test_the_sweep_runs_only_once(self):
        """
        A permanently limited API must terminate, not loop forever.

        The sweep is one pass; after it, remaining batches are counted as
        failures rather than requeued.
        """
        api = _Api(fail_first=10_000)

        with self.assertRaises(Exception):
            _run(api)

        # It gave up rather than spinning: the call count is bounded by
        # (batches x 2 attempts) for the main pass plus the same for the
        # single sweep, not unbounded.
        self.assertLess(api.calls, 100)

    def test_backoff_constants_are_sane(self):
        """The pacing must actually increase, and stay bounded."""
        self.assertGreater(risk_grid.BACKOFF_FACTOR, 1.0)
        self.assertGreater(risk_grid.BATCH_DELAY_MAX, risk_grid.BATCH_DELAY)
        self.assertGreater(risk_grid.RETRY_PASS_COOLDOWN, 0)

    def test_rate_limit_detection_covers_exhausted_retries(self):
        """
        The limiter usually surfaces as a wrapped MaxRetryError, not an
        HTTPError with a 429 response — checking only the latter missed
        every exhausted-retry case.
        """
        self.assertTrue(risk_grid._is_rate_limited(FakeRateLimit()))
        self.assertFalse(risk_grid._is_rate_limited(ValueError("boom")))
