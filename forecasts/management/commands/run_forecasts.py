"""
OrcaMet Portal — run_forecasts management command

Usage:
    python manage.py run_forecasts           # All active sites
    python manage.py run_forecasts --site 3  # Single site by ID

Exits non-zero when any site's forecast failed, so the Render cron shows the
run as failed instead of reporting success over a broken forecast.
"""

import logging
from django.core.management.base import BaseCommand, CommandError
from sites.models import Site
from forecasts.engine.runner import run_forecast_for_site, run_forecasts_all_active
from forecasts.locking import site_forecast_lock
from forecasts.models import ForecastRun

logger = logging.getLogger(__name__)


def _describe(run):
    """One line per run. A failed run has no peak risk to format."""
    if run.peak_risk is None:
        detail = run.error_message or "no forecast"
        return f"  {run.forecast_date}: [{run.status}] {detail}"
    return (
        f"  {run.forecast_date}: {run.recommendation} "
        f"(peak risk {run.peak_risk:.1f}%) [{run.status}]"
    )


class Command(BaseCommand):
    help = "Generate weather forecasts for active sites"

    def add_arguments(self, parser):
        parser.add_argument(
            "--site",
            type=int,
            help="Generate forecast for a specific site ID only",
        )

    def handle(self, *args, **options):
        site_id = options.get("site")

        if site_id:
            try:
                site = Site.objects.get(pk=site_id)
            except Site.DoesNotExist:
                raise CommandError(f"Site {site_id} not found")

            self.stdout.write(f"Generating forecast for: {site.name}")

            # Same lock as the cron and the background runs: all of them
            # delete-then-create this site's runs, and must not interleave.
            with site_forecast_lock(site.pk) as acquired:
                if not acquired:
                    raise CommandError(
                        f"A forecast for {site.name} is already running elsewhere"
                    )
                runs = run_forecast_for_site(site)

            for run in runs:
                self.stdout.write(_describe(run))

            if any(r.status == ForecastRun.Status.FAILED for r in runs):
                raise CommandError(f"Forecast failed for {site.name}")
        else:
            self.stdout.write("Generating forecasts for all active sites...")
            runs, errored = run_forecasts_all_active()
            success = sum(1 for r in runs if r.status == ForecastRun.Status.SUCCESS)
            failed = sum(1 for r in runs if r.status == ForecastRun.Status.FAILED)
            summary = f"Complete: {success} successful, {failed} failed"
            if errored:
                summary += f", {len(errored)} errored ({', '.join(errored)})"

            if failed or errored:
                raise CommandError(summary)
            self.stdout.write(self.style.SUCCESS(summary))
