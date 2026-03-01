"""
OrcaMet Portal — cleanup_forecasts management command

Removes forecast runs older than N days to keep the database lean.
Run daily via cron or manually: python manage.py cleanup_forecasts --days 30
"""

from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone

from forecasts.models import ForecastRun


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
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting",
        )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options["days"])

        old_runs = ForecastRun.objects.filter(generated_at__lt=cutoff)
        count = old_runs.count()

        if options["dry_run"]:
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
