"""
OrcaMet Portal — cleanup_forecasts management command

Removes forecast runs and UK grid runs older than N days to keep the
database lean. Runs on the site_forecasts cron every 6 hours, or manually:

    python manage.py cleanup_forecasts --days 30 --grid-days 2
"""

from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone

from forecasts.models import ForecastRun, UKRiskGridRun


class Command(BaseCommand):
    help = "Delete old forecast runs to keep the database lean"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Delete forecasts older than this many days (default: 30)",
        )
        parser.add_argument(
            "--grid-days",
            type=int,
            default=2,
            help=(
                "Delete UK risk grid runs older than this many days "
                "(default: 2, matching risk_grid's own retention window)"
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        cutoff = timezone.now() - timedelta(days=options["days"])
        old_runs = ForecastRun.objects.filter(generated_at__lt=cutoff)
        count = old_runs.count()

        if dry_run:
            self.stdout.write(
                f"Would delete {count} forecast runs older than {options['days']} days"
            )
        else:
            old_runs.delete()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Deleted {count} forecast runs older than {options['days']} days"
                )
            )

        # risk_grid prunes its own old runs, but only after a run succeeds.
        # A broken or failing grid job therefore stopped pruning at exactly
        # the moment data was still piling up — and grid points plus the
        # cached contour PNGs are the largest thing in this database. Sweeping
        # here too means retention keeps working from a different cron.
        grid_cutoff = timezone.localdate() - timedelta(days=options["grid_days"])
        old_grid = UKRiskGridRun.objects.filter(forecast_date__lt=grid_cutoff)
        grid_count = old_grid.count()

        if dry_run:
            self.stdout.write(
                f"Would delete {grid_count} UK grid runs older than "
                f"{options['grid_days']} days"
            )
        else:
            # Cascades to UKRiskGridPoint and CachedContourImage.
            old_grid.delete()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Deleted {grid_count} UK grid runs older than "
                    f"{options['grid_days']} days"
                )
            )
