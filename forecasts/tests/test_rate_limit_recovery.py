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
* the opening probe waits the limiter out instead of aborting the run

The probe is the whole run's single point of failure: it establishes the
timestamp axis every accumulator is sized against. It had no rate-limit
handling at all, so a live run died twelve seconds in, on a quota a previous
run had just spent, without fetching a single grid point.

Only the HTTP layer is replaced, so the real command drives these paths.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
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


class FakeDroppedConnection(Exception):
    """A transient blip that is not the limiter — no 429 anywhere in it."""

    def __init__(self):
        super().__init__(
            "('Connection aborted.', RemoteDisconnected("
            "'Remote end closed connection without response'))"
        )


class _ProbeApi:
    """Fails the probe `fail_first` times with `error`, then behaves."""

    def __init__(self, fail_first, error=FakeRateLimit):
        self.fail_first = fail_first
        self.error = error
        self.calls = 0
        self.probe_calls = 0
        self.probe_done = False

    def __call__(self, lats, lons, forecast_days=2, model=None):
        self.calls += 1

        # The probe is the run's first call; everything after it is a batch.
        if not self.probe_done:
            self.probe_calls += 1
            if self.probe_calls <= self.fail_first:
                raise self.error()
            self.probe_done = True

        return _members(lats)


class ProbeRateLimitTests(TestCase):

    def test_a_rate_limited_probe_is_waited_out_not_fatal(self):
        """
        The regression: two 429s on the probe used to end the run.

        Nothing was fetched, nothing was rendered, and the map kept serving
        the previous run's overlays.
        """
        api = _ProbeApi(fail_first=2)

        run = _run(api)

        self.assertEqual(run.status, UKRiskGridRun.Status.SUCCESS)
        self.assertTrue(UKRiskGridPoint.objects.filter(run=run).exists())

    def test_a_dropped_connection_is_also_retried(self):
        """Not every transient failure carries a 429."""
        api = _ProbeApi(fail_first=1, error=FakeDroppedConnection)

        run = _run(api)

        self.assertEqual(run.status, UKRiskGridRun.Status.SUCCESS)

    def test_a_probe_that_never_recovers_still_fails(self):
        """Patience is bounded: it must give up, not retry forever."""
        api = _ProbeApi(fail_first=10_000)

        with self.assertRaises(CommandError):
            _run(api)

        self.assertEqual(api.probe_calls, risk_grid.PROBE_ATTEMPTS)

    def test_the_failed_run_is_recorded(self):
        """A dead probe must leave a FAILED run, not a dangling RUNNING one."""
        api = _ProbeApi(fail_first=10_000)

        with self.assertRaises(CommandError):
            _run(api)

        run = UKRiskGridRun.objects.latest("id")
        self.assertEqual(run.status, UKRiskGridRun.Status.FAILED)
        self.assertIn("429", run.error_message)

    def test_an_empty_probe_is_not_retried(self):
        """
        A probe that answers with no members is not a transient failure.

        Retrying it would burn PROBE_ATTEMPTS worth of waiting on a response
        that is going to be just as empty next time.
        """
        calls = []

        def empty(lats, lons, forecast_days=2, model=None):
            calls.append(len(lats))
            return [], [None]

        with patch("forecasts.engine.ensemble.fetch_grid_members",
                   side_effect=empty),              patch("forecasts.management.commands.risk_grid.time.sleep"):
            with self.assertRaises(CommandError):
                call_command(
                    "risk_grid",
                    "--resolution", "4.0", "--days", "1",
                    "--contour-vars", "none", verbosity=0,
                )

        self.assertEqual(len(calls), 1, "an empty probe was retried")

    def test_the_wait_escalates_between_attempts(self):
        """
        Each 429 buys a longer pause than the last.

        Repeating a fixed wait against a spent quota just spends the
        attempts faster than the quota recovers.
        """
        api = _ProbeApi(fail_first=3)
        slept = []

        with patch("forecasts.engine.ensemble.fetch_grid_members",
                   side_effect=api),              patch("forecasts.management.commands.risk_grid.time.sleep",
                   side_effect=slept.append):
            call_command(
                "risk_grid",
                "--resolution", "4.0", "--days", "1",
                "--contour-vars", "none", verbosity=0,
            )

        waits = [s for s in slept if s >= risk_grid.RATE_LIMIT_WAIT]
        self.assertGreaterEqual(len(waits), 3)
        self.assertEqual(waits[:3], sorted(waits[:3]))
        self.assertGreater(waits[2], waits[0])

    def test_probe_constants_are_sane(self):
        self.assertGreaterEqual(risk_grid.PROBE_ATTEMPTS, 2)
        self.assertGreater(risk_grid.PROBE_RETRY_WAIT, 0)

        # Worst case must stay well inside a 6-hourly cron window.
        worst = sum(
            risk_grid.RATE_LIMIT_WAIT * risk_grid.BACKOFF_FACTOR ** i
            for i in range(risk_grid.PROBE_ATTEMPTS - 1)
        )
        self.assertLess(worst, 900)


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
