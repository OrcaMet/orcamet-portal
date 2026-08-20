"""
OrcaMet Portal — Site Signals

Automatically triggers forecast generation when a Site is created or updated.
Runs in a background thread to avoid blocking the admin save.
"""

import logging
import threading

from django.conf import settings
from django.db import connection, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from sites.models import Site

logger = logging.getLogger(__name__)

# Cap on forecast threads running inside one web worker.
#
# Each run pulls four models from Open-Meteo and does numpy work, in a
# gunicorn process on a 512 MB instance running WEB_CONCURRENCY=4. Trial
# accounts can trigger this from the browser — a site per add, and again on
# every edit — so without a ceiling a handful of testers could have a dozen
# runs in flight per worker. Over the cap the inline run is skipped and the
# scheduled cron picks the site up instead.
MAX_CONCURRENT = getattr(settings, "FORECAST_MAX_CONCURRENT_THREADS", 2)
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


def _generate_forecast_background(site_id: int):
    """Run forecast generation in a background thread."""
    from forecasts import locking

    try:
        # Import here to avoid circular imports
        from sites.models import Site
        from forecasts.engine.runner import run_forecast_for_site

        site = Site.objects.get(pk=site_id)

        if not site.is_active or site.job_complete:
            logger.info(f"Skipping forecast for {site.name} (inactive or complete)")
            return

        # `is None` rather than falsiness — longitude 0.0 is a valid UK location.
        if site.latitude is None or site.longitude is None:
            logger.warning(f"Skipping forecast for {site.name} (no coordinates)")
            return

        logger.info(f"Auto-generating forecast for {site.name}...")
        runs = run_forecast_for_site(site)
        logger.info(f"Auto-forecast complete for {site.name}: {len(runs)} day(s)")

    except Exception as e:
        logger.error(f"Auto-forecast failed for site {site_id}: {e}", exc_info=True)
    finally:
        # Release in this order: the lock is what other processes wait on.
        locking.release(site_id)
        _slots.release()
        # This thread opened its own DB connection; close it so it is not
        # left idle for conn_max_age after the thread exits.
        connection.close()


def queue_forecast_generation(site_id: int, site_name: str = "") -> bool:
    """
    Start a background forecast run for this site unless one is already
    running anywhere. Shared by the post_save signal below and the admin bulk
    action (sites/admin.py).

    Returns True if a run was started, False if it was skipped — either
    because another process already holds the site's lock, or because this
    worker is already at its thread ceiling.
    """
    from forecasts import locking

    # Cross-process first: this is the guard that actually prevents two
    # runs racing on the delete-then-create in run_forecast_for_site.
    if not locking.acquire(site_id):
        logger.info(
            f"Forecast already running for {site_name or site_id} — "
            f"not starting another"
        )
        return False

    if not _slots.acquire(blocking=False):
        logger.warning(
            f"At the {MAX_CONCURRENT}-run ceiling for this worker — leaving "
            f"{site_name or site_id} to the scheduled run"
        )
        locking.release(site_id)
        return False

    try:
        thread = threading.Thread(
            target=_generate_forecast_background,
            args=(site_id,),
            daemon=True,
        )
        thread.start()
    except Exception:
        # Nothing will reach the thread's finally, so undo both here.
        _slots.release()
        locking.release(site_id)
        raise

    return True


@receiver(post_save, sender=Site)
def trigger_forecast_on_site_save(sender, instance, created, **kwargs):
    """
    When a site is saved (created or updated), generate forecasts
    in a background thread so the admin doesn't hang.
    """
    # Only trigger if the site has coordinates and is active.
    # `is None` rather than falsiness — longitude 0.0 is a valid UK location.
    if instance.latitude is None or instance.longitude is None:
        return
    if not instance.is_active or instance.job_complete:
        return

    site_id = instance.pk
    site_name = instance.name

    action = "created" if created else "updated"
    logger.info(f"Site {action}: {site_name} — queuing forecast generation")

    # Wait for the surrounding transaction to commit. Otherwise the thread can
    # query the site before the write is visible (or at all, if it rolls
    # back) — and if it rolls back, claiming the in-flight slot immediately
    # in the signal body would leave the site stuck marked as in-flight
    # forever, since nothing would ever clear it.
    transaction.on_commit(lambda: queue_forecast_generation(site_id, site_name))
