"""
OrcaMet Portal — generate_contour_cache management command

Pre-renders contour map PNGs for every (timestamp × variable) combination
from the latest risk grid run, storing them in the CachedContourImage model
for instant map animation.

Run after risk_grid completes:
    python manage.py risk_grid && python manage.py generate_contour_cache

Or configure as sequential cron jobs on Render:
    1. python manage.py risk_grid
    2. python manage.py generate_contour_cache

Usage:
    python manage.py generate_contour_cache                  # Latest run
    python manage.py generate_contour_cache --resolution 300 # Custom res
    python manage.py generate_contour_cache --variables risk wind gust
"""

import logging
import time
from datetime import datetime, timezone

import numpy as np
from django.core.management.base import BaseCommand
from django.db.models import Max, Min

from forecasts.models import UKRiskGridRun, UKRiskGridPoint, CachedContourImage

logger = logging.getLogger(__name__)

# Variables to pre-render
ALL_VARIABLES = ["risk", "wind", "gust", "precip", "temp"]

# Map variable name -> model field + aggregation for peak maps
VARIABLE_FIELD_MAP = {
    "risk": "risk",
    "wind": "wind_speed",
    "gust": "wind_gusts",
    "precip": "precipitation",
    "temp": "temperature",
}


class Command(BaseCommand):
    help = "Pre-render contour map PNGs for instant map animation"

    def add_arguments(self, parser):
        parser.add_argument(
            "--resolution",
            type=int,
            default=300,
            help="Interpolation resolution (default: 300)",
        )
        parser.add_argument(
            "--variables",
            nargs="+",
            default=ALL_VARIABLES,
            help=f"Variables to render (default: {' '.join(ALL_VARIABLES)})",
        )
        parser.add_argument(
            "--run-id",
            type=int,
            default=None,
            help="Specific UKRiskGridRun ID (default: latest successful)",
        )
        parser.add_argument(
            "--dpi",
            type=int,
            default=150,
            help="Image DPI (default: 150)",
        )

    def handle(self, *args, **options):
        resolution = options["resolution"]
        variables = options["variables"]
        run_id = options["run_id"]
        dpi = options["dpi"]

        # Lazy import to avoid loading matplotlib at startup
        from forecasts.engine.map_interpolation import render_contour_to_bytes

        # Find the grid run
        if run_id:
            try:
                grid_run = UKRiskGridRun.objects.get(pk=run_id)
            except UKRiskGridRun.DoesNotExist:
                self.stderr.write(
                    self.style.ERROR(f"Grid run {run_id} not found")
                )
                return
        else:
            grid_run = (
                UKRiskGridRun.objects.filter(
                    status=UKRiskGridRun.Status.SUCCESS
                )
                .order_by("-generated_at")
                .first()
            )

        if not grid_run:
            self.stderr.write(
                self.style.ERROR("No successful grid run found")
            )
            return

        # Get all unique timestamps for this run
        timestamps = list(
            UKRiskGridPoint.objects.filter(run=grid_run)
            .values_list("timestamp", flat=True)
            .distinct()
            .order_by("timestamp")
        )

        if not timestamps:
            self.stderr.write(
                self.style.ERROR(
                    f"No grid points found for run {grid_run.pk}"
                )
            )
            return

        total_images = len(timestamps) * len(variables)
        self.stdout.write(
            f"Pre-rendering contour cache for grid run {grid_run.pk}\n"
            f"  Forecast date: {grid_run.forecast_date}\n"
            f"  Timestamps: {len(timestamps)}\n"
            f"  Variables: {', '.join(variables)}\n"
            f"  Total images: {total_images}\n"
            f"  Resolution: {resolution}, DPI: {dpi}"
        )

        # Delete existing cache for this run
        deleted_count, _ = CachedContourImage.objects.filter(
            run=grid_run
        ).delete()
        if deleted_count:
            self.stdout.write(
                f"  Cleared {deleted_count} existing cached images"
            )

        # Pre-fetch all unique (lat, lon) coordinates for this run
        # (same for every timestamp)
        start_time = time.time()
        rendered = 0
        failed = 0
        cache_records = []

        for var_idx, var_name in enumerate(variables):
            field_name = VARIABLE_FIELD_MAP.get(var_name, "risk")

            self.stdout.write(
                f"\n  [{var_idx + 1}/{len(variables)}] "
                f"Rendering {var_name}..."
            )

            for ts_idx, timestamp in enumerate(timestamps):
                try:
                    # Query grid points for this timestamp
                    points = UKRiskGridPoint.objects.filter(
                        run=grid_run,
                        timestamp=timestamp,
                    )

                    lats = np.array(
                        list(points.values_list("latitude", flat=True))
                    )
                    lons = np.array(
                        list(points.values_list("longitude", flat=True))
                    )
                    values = np.array(
                        list(points.values_list(field_name, flat=True))
                    )

                    if len(lats) < 4:
                        logger.warning(
                            f"Skipping {var_name} @ {timestamp}: "
                            f"only {len(lats)} points"
                        )
                        failed += 1
                        continue

                    # Render the contour image
                    png_bytes = render_contour_to_bytes(
                        lats, lons, values,
                        variable=var_name,
                        resolution=resolution,
                        dpi=dpi,
                    )

                    cache_records.append(CachedContourImage(
                        run=grid_run,
                        timestamp=timestamp,
                        variable=var_name,
                        image_data=png_bytes,
                    ))

                    rendered += 1

                    # Progress
                    if (ts_idx + 1) % 12 == 0 or ts_idx == len(timestamps) - 1:
                        elapsed = time.time() - start_time
                        total_done = rendered + failed
                        rate = total_done / elapsed if elapsed > 0 else 0
                        remaining = total_images - total_done
                        eta = remaining / rate if rate > 0 else 0
                        self.stdout.write(
                            f"    {ts_idx + 1}/{len(timestamps)} hours — "
                            f"{rendered} rendered, "
                            f"{rate:.1f} img/s, ETA {eta:.0f}s"
                        )

                except Exception as e:
                    logger.error(
                        f"Failed to render {var_name} @ {timestamp}: {e}"
                    )
                    failed += 1

        # Bulk insert all cached images
        if cache_records:
            self.stdout.write(
                f"\n  Storing {len(cache_records)} cached images..."
            )
            try:
                # Insert in batches to avoid memory issues
                batch_size = 50
                for i in range(0, len(cache_records), batch_size):
                    batch = cache_records[i:i + batch_size]
                    CachedContourImage.objects.bulk_create(batch)

                elapsed = time.time() - start_time
                avg_size = sum(
                    len(r.image_data) for r in cache_records
                ) / len(cache_records)

                self.stdout.write(self.style.SUCCESS(
                    f"\n  ✓ Contour cache complete: "
                    f"{len(cache_records)} images "
                    f"({failed} failed) in {elapsed:.0f}s\n"
                    f"  Average image size: {avg_size / 1024:.0f} KB\n"
                    f"  Total cache size: "
                    f"{sum(len(r.image_data) for r in cache_records) / 1024 / 1024:.1f} MB"
                ))

            except Exception as e:
                logger.error(f"Bulk insert failed: {e}")
                self.stderr.write(
                    self.style.ERROR(f"  ✗ Storage failed: {e}")
                )
        else:
            self.stderr.write(
                self.style.ERROR("  ✗ No images were rendered")
            )
