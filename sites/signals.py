"""
OrcaMet Portal — Site Signals

Automatically triggers forecast generation when a Site is created or updated.
Runs in a background thread to avoid blocking the admin save.
"""

import logging
import threading

from django.conf import settings
from django.db import connection, transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from sites.models import Site, ThresholdProfile

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


# Fields a forecast actually depends on, or that switch forecasting back on.
# Name, notes and elevation (which the engine does not read) are not here, so
# a trial user fixing a typo no longer costs four Open-Meteo calls and a slot
# on the worker's thread ceiling.
FORECAST_FIELDS = (
    "postcode", "latitude", "longitude", "exposure", "is_active", "job_complete",
)


@receiver(pre_save, sender=Site)
def note_forecast_relevant_change(sender, instance, **kwargs):
    """
    Record on the instance whether this save changes anything a forecast
    depends on, for trigger_forecast_on_site_save to read.
    """
    if instance.pk is None:
        instance._forecast_inputs_changed = True
        return

    update_fields = kwargs.get("update_fields")
    fields = FORECAST_FIELDS
    if update_fields is not None:
        fields = tuple(f for f in FORECAST_FIELDS if f in update_fields)
        if not fields:
            instance._forecast_inputs_changed = False
            return

    previous = Site.objects.filter(pk=instance.pk).values(*fields).first()
    instance._forecast_inputs_changed = previous is None or any(
        previous[f] != getattr(instance, f) for f in fields
    )


@receiver(post_save, sender=Site)
def trigger_forecast_on_site_save(sender, instance, created, **kwargs):
    """
    When a site is created, or updated in a way that changes its forecast,
    generate forecasts in a background thread so the admin doesn't hang.

    Staff who want a fresh run without changing anything use the admin's
    "Generate forecasts" action, which calls queue_forecast_generation
    directly.
    """
    # Only trigger if the site has coordinates and is active.
    # `is None` rather than falsiness — longitude 0.0 is a valid UK location.
    if instance.latitude is None or instance.longitude is None:
        return
    if not instance.is_active or instance.job_complete:
        return
    # Default True: a save that bypassed pre_save should still forecast, as
    # every save did before this check existed.
    if not created and not getattr(instance, "_forecast_inputs_changed", True):
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


@receiver(post_save, sender=ThresholdProfile)
def trigger_forecast_on_threshold_save(sender, instance, **kwargs):
    """
    Re-forecast a site when its active limits change.

    Every stored verdict and chance of cancellation is scored against the
    limits in force when it was generated. Without this, editing a profile in
    the admin changed nothing until the next cron run, up to six hours later
    — while the site page already drew the new limit lines over verdicts
    scored against the old ones.

    On a brand new site this fires alongside the site's own post_save. The
    second of the two finds the site's forecast lock held and is skipped, so
    it still costs one run, not two.
    """
    if not instance.is_active:
        return

    site = instance.site
    # `is None` rather than falsiness — longitude 0.0 is a valid UK location.
    if site.latitude is None or site.longitude is None:
        return
    if not site.is_active or site.job_complete:
        return

    site_id = site.pk
    site_name = site.name
    logger.info(f"Thresholds saved for {site_name} — queuing forecast generation")

    # After commit, for the same reasons as the site signal above.
    transaction.on_commit(lambda: queue_forecast_generation(site_id, site_name))
