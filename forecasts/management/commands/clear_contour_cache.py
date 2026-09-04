"""
OrcaMet Portal — clear_contour_cache management command

Deletes the pre-rendered contour PNGs in CachedContourImage.

The map paints its own field in the browser now, from the grid point values,
so nothing serves these images and risk_grid no longer writes them. They are
the largest thing in this database — roughly 250 KB a frame, five variables
across a 72 hour horizon — and they will otherwise sit there until each run
ages out of the retention window.

Dry run by default. Deleting is irreversible and these images can only be
recreated by re-running risk_grid with --contour-vars, so the delete has to
be asked for explicitly:

    python manage.py clear_contour_cache            # report only
    python manage.py clear_contour_cache --delete   # actually delete

--keep-latest holds back the most recent successful run's images, which is
worth doing if backing out to the server-rendered overlay is still on the
table.
"""

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Count

from forecasts.models import CachedContourImage, UKRiskGridRun


def _human(num_bytes):
    """Bytes as something a person can read at a glance."""
    if num_bytes is None:
        return "unknown"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class Command(BaseCommand):
    help = "Delete the pre-rendered contour PNGs the map no longer uses"

    def add_arguments(self, parser):
        parser.add_argument(
            "--delete",
            action="store_true",
            help=(
                "Actually delete. Without this the command only reports what "
                "it would remove."
            ),
        )
        parser.add_argument(
            "--keep-latest",
            action="store_true",
            help=(
                "Keep the images belonging to the most recent successful grid "
                "run, so the server-rendered overlay stays available for that "
                "run if the client-side renderer has to be backed out."
            ),
        )

    def _measure(self, queryset):
        """
        Total stored bytes for these images.

        Summing in Python would mean pulling every BLOB through the process
        to measure it, which is the opposite of the point. Postgres can
        measure the column in place; anything else (SQLite in the test suite)
        falls back to a count and no size.
        """
        if connection.vendor != "postgresql":
            return None

        ids = list(queryset.values_list("pk", flat=True))
        if not ids:
            return 0

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(OCTET_LENGTH(image_data)), 0) "
                "FROM forecasts_cachedcontourimage WHERE id = ANY(%s)",
                [ids],
            )
            return cursor.fetchone()[0]

    def handle(self, *args, **options):
        images = CachedContourImage.objects.all()

        kept_run = None
        if options["keep_latest"]:
            kept_run = (
                UKRiskGridRun.objects
                .filter(status=UKRiskGridRun.Status.SUCCESS)
                .order_by("-generated_at")
                .first()
            )
            if kept_run:
                images = images.exclude(run=kept_run)

        total = images.count()

        if not total:
            self.stdout.write("No contour images to remove.")
            return

        # Per-run breakdown, so the report says what is actually being lost
        # rather than only how much of it there is.
        by_run = (
            images.values("run_id", "run__forecast_date")
            .annotate(n=Count("id"))
            .order_by("run__forecast_date")
        )

        self.stdout.write(f"\n  Contour images to remove: {total}")
        for row in by_run:
            self.stdout.write(
                f"    run {row['run_id']} ({row['run__forecast_date']}): "
                f"{row['n']} image(s)"
            )

        size = self._measure(images)
        if size is not None:
            self.stdout.write(f"  Stored size: {_human(size)}")

        if kept_run:
            kept = CachedContourImage.objects.filter(run=kept_run).count()
            self.stdout.write(
                f"  Keeping {kept} image(s) from run {kept_run.pk} "
                f"({kept_run.forecast_date})"
            )

        if not options["delete"]:
            self.stdout.write(self.style.WARNING(
                "\n  Dry run — nothing deleted. Re-run with --delete to "
                "remove them."
            ))
            return

        deleted, _ = images.delete()
        self.stdout.write(self.style.SUCCESS(
            f"\n  Deleted {total} contour image(s) ({deleted} rows)."
        ))

        if connection.vendor == "postgresql":
            # Deleting rows marks the space reusable by this table; it does
            # not hand it back to the filesystem. Anyone watching the disk
            # figure expecting it to drop should know why it has not.
            self.stdout.write(
                "  Note: Postgres frees this space for reuse within the "
                "table, but does not return it to the disk without a "
                "VACUUM FULL — which locks the table, so it is worth doing "
                "deliberately rather than as part of this command."
            )
