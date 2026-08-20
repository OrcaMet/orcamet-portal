"""
Tests for cross-process forecast de-duplication.

The guard these replace was a module-level set, which could only ever see
one process. Production runs four gunicorn workers plus a cron, so these
tests care specifically about the case where the *caller* is not the one
holding the lock.
"""

import threading
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from forecasts import locking
from forecasts.models import ForecastLock
from sites.models import Client, Site
from sites.signals import queue_forecast_generation


def make_site(name="Tower"):
    client, _ = Client.objects.get_or_create(name="Acme Rope")
    return Site.objects.create(
        client=client, name=name, postcode="EH1 1YZ",
        latitude=55.95, longitude=-3.19,
    )


class LockPrimitiveTests(TestCase):
    def setUp(self):
        self.site = make_site()

    def test_acquire_succeeds_when_free(self):
        self.assertTrue(locking.acquire(self.site.pk))
        self.assertTrue(ForecastLock.objects.filter(site=self.site).exists())

    def test_second_acquire_is_refused(self):
        locking.acquire(self.site.pk)
        self.assertFalse(locking.acquire(self.site.pk))

    def test_release_allows_reacquisition(self):
        locking.acquire(self.site.pk)
        locking.release(self.site.pk)

        self.assertFalse(ForecastLock.objects.filter(site=self.site).exists())
        self.assertTrue(locking.acquire(self.site.pk))

    def test_release_is_safe_when_not_held(self):
        locking.release(self.site.pk)  # must not raise

    def test_locks_are_per_site(self):
        other = make_site("Other Tower")
        locking.acquire(self.site.pk)
        self.assertTrue(locking.acquire(other.pk))

    def test_stale_lock_is_reclaimed(self):
        """
        Background runs are daemon threads, so a deploy kills them mid-flight
        and leaves the row behind. Without this the site would never forecast
        again.
        """
        locking.acquire(self.site.pk)
        ForecastLock.objects.filter(site=self.site).update(
            acquired_at=timezone.now() - locking.STALE_AFTER - timedelta(minutes=1)
        )

        self.assertTrue(locking.acquire(self.site.pk))

    def test_fresh_lock_is_not_reclaimed(self):
        locking.acquire(self.site.pk)
        ForecastLock.objects.filter(site=self.site).update(
            acquired_at=timezone.now() - locking.STALE_AFTER + timedelta(minutes=5)
        )

        self.assertFalse(locking.acquire(self.site.pk))

    def test_context_manager_releases_on_exception(self):
        with self.assertRaises(RuntimeError):
            with locking.site_forecast_lock(self.site.pk) as acquired:
                self.assertTrue(acquired)
                raise RuntimeError("boom")

        self.assertFalse(ForecastLock.objects.filter(site=self.site).exists())

    def test_context_manager_does_not_release_a_lock_it_did_not_take(self):
        """A losing caller must not free the winner's lock."""
        locking.acquire(self.site.pk)

        with locking.site_forecast_lock(self.site.pk) as acquired:
            self.assertFalse(acquired)

        self.assertTrue(ForecastLock.objects.filter(site=self.site).exists())

    def test_lock_is_removed_when_the_site_is_deleted(self):
        locking.acquire(self.site.pk)
        self.site.delete()
        self.assertEqual(ForecastLock.objects.count(), 0)


class QueueForecastGenerationTests(TestCase):
    """The decision logic, with no real thread started."""

    def setUp(self):
        self.site = make_site()

    def test_starts_a_run_when_free(self):
        with patch("sites.signals.threading.Thread") as thread:
            self.assertTrue(queue_forecast_generation(self.site.pk, self.site.name))
        thread.return_value.start.assert_called_once()

    def test_refuses_when_another_process_holds_the_lock(self):
        """
        The case the old in-memory set could not see: the lock was taken by a
        different process entirely.
        """
        locking.acquire(self.site.pk)

        with patch("sites.signals.threading.Thread") as thread:
            self.assertFalse(queue_forecast_generation(self.site.pk, self.site.name))

        thread.assert_not_called()

    def test_lock_is_released_if_the_thread_cannot_start(self):
        with patch("sites.signals.threading.Thread", side_effect=RuntimeError("no threads")):
            with self.assertRaises(RuntimeError):
                queue_forecast_generation(self.site.pk, self.site.name)

        self.assertFalse(ForecastLock.objects.filter(site=self.site).exists())

    def test_worker_thread_ceiling_skips_the_inline_run(self):
        """
        Trial accounts can trigger runs from the browser, so the number of
        concurrent runs per worker has to be bounded.
        """
        other = make_site("Second Tower")

        with patch("sites.signals._slots", threading.BoundedSemaphore(1)), \
                patch("sites.signals.threading.Thread"):
            self.assertTrue(queue_forecast_generation(self.site.pk, self.site.name))
            # The slot is still held (no real thread ran to release it).
            self.assertFalse(queue_forecast_generation(other.pk, other.name))

        # The skipped site must not be left locked — the cron has to be able
        # to pick it up.
        self.assertFalse(ForecastLock.objects.filter(site=other).exists())


class CronSkipsLockedSitesTests(TestCase):
    def test_all_active_skips_a_site_already_running_elsewhere(self):
        from forecasts.engine.runner import run_forecasts_all_active

        busy = make_site("Busy Tower")
        free = make_site("Free Tower")
        locking.acquire(busy.pk)

        seen = []
        with patch(
            "forecasts.engine.runner.run_forecast_for_site",
            side_effect=lambda s: seen.append(s.pk) or [],
        ):
            run_forecasts_all_active()

        self.assertEqual(seen, [free.pk])

    def test_all_active_releases_the_locks_it_takes(self):
        from forecasts.engine.runner import run_forecasts_all_active

        make_site("Lonely Tower")

        with patch("forecasts.engine.runner.run_forecast_for_site", return_value=[]):
            run_forecasts_all_active()

        self.assertEqual(ForecastLock.objects.count(), 0)
