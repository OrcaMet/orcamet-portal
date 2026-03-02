"""
OrcaMet Portal - generate_risk_map management command

Reads the latest UKRiskGridRun data and generates a smooth,
coastline-clipped contour map using CloughTocher2D interpolation.
Stores the result in UKRiskMap.
"""

import logging
from datetime import datetime, timezone

from django.core.management.base import BaseCommand
from django.db.models import Max

from forecasts.models import UKRiskGridRun, UKRiskGridPoint, UKRiskMap

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Generate a smooth UK risk contour map from grid data"

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-id",
            type=int,
            default=None,
            help="Use a specific UKRiskGridRun ID. Default: latest successful run.",
        )
        parser.add_argument(
            "--resolution",
            type=int,
            default=300,
            help="Interpolation resolution (300=fast, 500=detailed, 800=pub, 1000=max)",
        )
        parser.add_argument(
            "--hour",
            type=int,
            default=None,
            help="Generate map for a specific UTC hour (0-23). Default: peak across all hours.",
        )

    def handle(self, *args, **options):
        run_id = options["run_id"]
        resolution = options["resolution"]
        target_hour = options["hour"]

        try:
            from forecasts.engine.map_interpolation import (
                generate_uk_risk_map,
                INTERP_RESOLUTION,
            )
        except ImportError as e:
            self.stderr.write(
                self.style.ERROR(
                    f"Missing dependencies for map generation: {e}\n"
                    f"Run: pip install scipy matplotlib geopandas shapely"
                )
            )
            return

        import numpy as np

        # Find the grid run to use
        if run_id:
            try:
                grid_run = UKRiskGridRun.objects.get(pk=run_id)
            except UKRiskGridRun.DoesNotExist:
                self.stderr.write(f"Grid run {run_id} not found")
                return
        else:
            grid_run = (
                UKRiskGridRun.objects
                .filter(status=UKRiskGridRun.Status.SUCCESS)
                .order_by("-generated_at")
                .first()
            )
            if not grid_run:
                self.stderr.write(
                    self.style.ERROR(
                        "No successful grid runs found. "
                        "Run 'python manage.py generate_risk_grid' first."
                    )
                )
                return

        self.stdout.write(
            f"Using grid run #{grid_run.pk}: {grid_run.forecast_date} "
            f"({grid_run.grid_points} points, {grid_run.get_status_display()})"
        )

        points_qs = UKRiskGridPoint.objects.filter(run=grid_run)

        if not points_qs.exists():
            self.stderr.write(self.style.ERROR("No grid points in this run"))
            return

        if target_hour is not None:
            points_qs = points_qs.filter(timestamp__hour=target_hour)
            if not points_qs.exists():
                self.stderr.write(
                    self.style.ERROR(f"No data for hour {target_hour:02d}:00 UTC")
                )
                return

            lats = np.array(list(points_qs.values_list("latitude", flat=True)))
            lons = np.array(list(points_qs.values_list("longitude", flat=True)))
            risks = np.array(list(points_qs.values_list("risk", flat=True)))

            subtitle = (
                f"{grid_run.forecast_date.strftime('%A %d %B %Y')} "
                f"- {target_hour:02d}:00 UTC"
            )
            title = "UK Construction Risk"
        else:
            peak_data = (
                points_qs
                .values("latitude", "longitude")
                .annotate(peak_risk=Max("risk"))
            )

            lats = np.array([p["latitude"] for p in peak_data])
            lons = np.array([p["longitude"] for p in peak_data])
            risks = np.array([p["peak_risk"] for p in peak_data])

            subtitle = grid_run.forecast_date.strftime("%A %d %B %Y")
            title = "UK Construction Risk - Peak Exceedance"

        self.stdout.write(
            f"  {len(lats)} data points, risk range: "
            f"{risks.min():.1f}% - {risks.max():.1f}%"
        )
        self.stdout.write(f"  Interpolation resolution: {resolution}")

        try:
            b64_png = generate_uk_risk_map(
                lats, lons, risks,
                resolution=resolution,
                title=title,
                forecast_date=subtitle,
            )
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Map generation failed: {e}"))
            logger.error(f"Map generation failed: {e}", exc_info=True)
            return

        peak_idx = np.argmax(risks)
        peak_lat = float(lats[peak_idx])
        peak_lon = float(lons[peak_idx])
        peak_risk = float(risks[peak_idx])

        risk_map = UKRiskMap.objects.create(
            forecast_date=grid_run.forecast_date,
            image_data=b64_png,
            peak_risk=peak_risk,
            peak_location_lat=peak_lat,
            peak_location_lon=peak_lon,
            grid_points=len(lats),
        )

        img_size_kb = len(b64_png) // 1024

        self.stdout.write(
            self.style.SUCCESS(
                f"  Map generated and stored (UKRiskMap #{risk_map.pk})\n"
                f"    Image: {img_size_kb}KB base64 PNG\n"
                f"    Peak: {peak_risk:.1f}% at ({peak_lat:.2f}N, {peak_lon:.2f}E)"
            )
        )
