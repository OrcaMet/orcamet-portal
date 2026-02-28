"""
OrcaMet Portal — run_forecasts management command

Usage:
    python manage.py run_forecasts           # All active sites
    python manage.py run_forecasts --site 3  # Single site by ID
"""

import logging
from django.core.management.base import BaseCommand
from sites.models import Site
from forecasts.engine.runner import run_forecast_for_site, run_forecasts_all_active

logger = logging.getLogger(__name__)


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
                self.stderr.write(f"Site {site_id} not found")
                return

            self.stdout.write(f"Generating forecast for: {site.name}")
            runs = run_forecast_for_site(site)
            for run in runs:
                self.stdout.write(
                    f"  {run.forecast_date}: {run.recommendation} "
                    f"(peak risk {run.peak_risk:.1f}%) [{run.status}]"
                )
        else:
            self.stdout.write("Generating forecasts for all active sites...")
            runs = run_forecasts_all_active()
            success = sum(1 for r in runs if r.status == "success")
            failed = sum(1 for r in runs if r.status == "failed")
            self.stdout.write(
                self.style.SUCCESS(f"Complete: {success} successful, {failed} failed")
            )
