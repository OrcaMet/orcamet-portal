"""
OrcaMet Portal — Site Signals

Automatically triggers forecast generation when a Site is created or updated.
Runs in a background thread to avoid blocking the admin save.
"""

import logging
import threading

from django.db.models.signals import post_save
from django.dispatch import receiver

from sites.models import Site

logger = logging.getLogger(__name__)


def _generate_forecast_background(site_id: int):
    """Run forecast generation in a background thread."""
    try:
        # Import here to avoid circular imports
        from sites.models import Site
        from forecasts.engine.runner import run_forecast_for_site

        site = Site.objects.get(pk=site_id)

        if not site.is_active or site.job_complete:
            logger.info(f"Skipping forecast for {site.name} (inactive or complete)")
            return

        if not site.latitude or not site.longitude:
            logger.warning(f"Skipping forecast for {site.name} (no coordinates)")
            return

        logger.info(f"Auto-generating forecast for {site.name}...")
        runs = run_forecast_for_site(site)
        logger.info(f"Auto-forecast complete for {site.name}: {len(runs)} day(s)")

    except Exception as e:
        logger.error(f"Auto-forecast failed for site {site_id}: {e}", exc_info=True)


@receiver(post_save, sender=Site)
def trigger_forecast_on_site_save(sender, instance, created, **kwargs):
    """
    When a site is saved (created or updated), generate forecasts
    in a background thread so the admin doesn't hang.
    """
    # Only trigger if the site has coordinates and is active
    if not instance.latitude or not instance.longitude:
        return
    if not instance.is_active or instance.job_complete:
        return

    action = "created" if created else "updated"
    logger.info(f"Site {action}: {instance.name} — triggering forecast generation")

    thread = threading.Thread(
        target=_generate_forecast_background,
        args=(instance.pk,),
        daemon=True,
    )
    thread.start()
