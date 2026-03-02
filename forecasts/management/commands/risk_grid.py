"""
OrcaMet Portal — generate_risk_grid management command

Fetches weather data for a grid of points across the UK, computes
hourly risk scores at each point, and stores the results for the
interactive map heatmap layer.

Usage:
    python manage.py generate_risk_grid                     # Default 0.5° grid
    python manage.py generate_risk_grid --resolution 0.25   # Finer grid
    python manage.py generate_risk_grid --days 2            # 2-day forecast
"""

import logging
import time
from datetime import datetime, timezone, timedelta

import numpy as np
from django.core.management.base import BaseCommand
from django.conf import settings

from forecasts.models import UKRiskGridRun, UKRiskGridPoint
from forecasts.engine.core import (
    fetch_single_model,
    calculate_hourly_risk,
    get_model_weights,
    MODELS_CONFIG,
)

logger = logging.getLogger(__name__)

# UK bounding box (covers mainland GB + Northern Ireland)
UK_LAT_MIN = 49.9
UK_LAT_MAX = 58.7
UK_LON_MIN = -7.6
UK_LON_MAX = 1.8

# Default thresholds for the grid (generic — no site-specific exposure)
DEFAULT_THRESHOLDS = {
    "wind_mean_caution": 10.0,
    "wind_mean_cancel": 14.0,
    "gust_caution": 15.0,
    "gust_cancel": 20.0,
    "precip_caution": 0.7,
    "precip_cancel": 2.0,
    "temp_min_caution": 1.0,
    "temp_min_cancel": -2.0,
}


class Command(BaseCommand):
    help = "Generate UK-wide risk grid for the interactive map heatmap"

    def add_arguments(self, parser):
        parser.add_argument(
            "--resolution",
            type=float,
            default=0.5,
            help="Grid spacing in degrees (default: 0.5 ≈ 55km)",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=3,
            help="Number of forecast days (default: 3)",
        )
        parser.add_argument(
            "--model",
            type=str,
            default="ecmwf",
            help="Single model to use for the grid (default: ecmwf). "
                 "Using a single model keeps API calls manageable.",
        )

    def handle(self, *args, **options):
        resolution = options["resolution"]
        num_days = options["days"]
        model_name = options["model"]

        if model_name not in MODELS_CONFIG:
            self.stderr.write(
                f"Unknown model '{model_name}'. "
                f"Available: {', '.join(MODELS_CONFIG.keys())}"
            )
            return

        # Build the grid
        lats = np.arange(UK_LAT_MIN, UK_LAT_MAX + resolution, resolution)
        lons = np.arange(UK_LON_MIN, UK_LON_MAX + resolution, resolution)
        grid_points = [(float(lat), float(lon)) for lat in lats for lon in lons]
        total_points = len(grid_points)

        today = datetime.now(timezone.utc).date()
        end_date = today + timedelta(days=num_days - 1)

        self.stdout.write(
            f"Generating UK risk grid: {len(lats)}×{len(lons)} = "
            f"{total_points} points at {resolution}° resolution"
        )
        self.stdout.write(
            f"  Model: {MODELS_CONFIG[model_name]['name']}"
        )
        self.stdout.write(
            f"  Period: {today} to {end_date} ({num_days} days)"
        )

        # Create the run record
        grid_run = UKRiskGridRun.objects.create(
            forecast_date=today,
            status=UKRiskGridRun.Status.RUNNING,
            lat_min=UK_LAT_MIN,
            lat_max=UK_LAT_MAX,
            lon_min=UK_LON_MIN,
            lon_max=UK_LON_MAX,
            resolution=resolution,
            grid_points=total_points,
            models_used=[model_name],
        )

        # Delete any previous grid data for today (replace strategy)
        UKRiskGridRun.objects.filter(
            forecast_date=today,
            status=UKRiskGridRun.Status.SUCCESS,
        ).exclude(pk=grid_run.pk).delete()

        all_point_records = []
        failed_points = 0
        start_time = time.time()

        for idx, (lat, lon) in enumerate(grid_points, 1):
            if idx % 10 == 0 or idx == 1:
                elapsed = time.time() - start_time
                rate = idx / elapsed if elapsed > 0 else 0
                eta = (total_points - idx) / rate if rate > 0 else 0
                self.stdout.write(
                    f"  [{idx}/{total_points}] "
                    f"({lat:.2f}°N, {lon:.2f}°E) "
                    f"— {rate:.1f} pts/s, ETA {eta:.0f}s",
                    ending="\r",
                )

            try:
                data = fetch_single_model(
                    model_name=model_name,
                    lat=lat,
                    lon=lon,
                    start_date=today.strftime("%Y-%m-%d"),
                    end_date=end_date.strftime("%Y-%m-%d"),
                )

                times = data["time"]
                winds = data.get("wind_speed", [])
                gusts = data.get("wind_gusts", [])
                precips = data.get("precipitation", [])
                temps = data.get("temperature", [])

                for i, t_str in enumerate(times):
                    w = float(winds[i]) if i < len(winds) and winds[i] is not None else 0.0
                    g = float(gusts[i]) if i < len(gusts) and gusts[i] is not None else 0.0
                    p = float(precips[i]) if i < len(precips) and precips[i] is not None else 0.0
                    temp = float(temps[i]) if i < len(temps) and temps[i] is not None else 10.0

                    # Sanitise NaN/inf
                    w = 0.0 if (np.isnan(w) or np.isinf(w)) else w
                    g = 0.0 if (np.isnan(g) or np.isinf(g)) else g
                    p = 0.0 if (np.isnan(p) or np.isinf(p)) else p
                    temp = 10.0 if (np.isnan(temp) or np.isinf(temp)) else temp

                    risk = calculate_hourly_risk(w, g, p, temp, DEFAULT_THRESHOLDS)

                    all_point_records.append(UKRiskGridPoint(
                        run=grid_run,
                        latitude=lat,
                        longitude=lon,
                        timestamp=datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                        if "T" in t_str
                        else datetime.strptime(t_str, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc),
                        wind_speed=round(w, 2),
                        wind_gusts=round(g, 2),
                        precipitation=round(p, 2),
                        temperature=round(temp, 2),
                        risk=round(risk, 2),
                    ))

            except Exception as e:
                logger.warning(f"  ✗ Grid point ({lat:.2f}, {lon:.2f}) failed: {e}")
                failed_points += 1

            # API rate limiting — be polite
            time.sleep(0.1)

        self.stdout.write("")  # Clear the \r line

        # Bulk insert all points
        if all_point_records:
            self.stdout.write(f"  Storing {len(all_point_records)} grid point records...")
            try:
                # Batch in chunks of 5000 to avoid memory issues
                batch_size = 5000
                for i in range(0, len(all_point_records), batch_size):
                    batch = all_point_records[i:i + batch_size]
                    UKRiskGridPoint.objects.bulk_create(batch)

                grid_run.status = UKRiskGridRun.Status.SUCCESS
                grid_run.num_hours = len(all_point_records) // max(total_points - failed_points, 1)
                grid_run.save()

                elapsed = time.time() - start_time
                self.stdout.write(self.style.SUCCESS(
                    f"  ✓ Complete: {len(all_point_records)} records "
                    f"({total_points - failed_points} points, {failed_points} failed) "
                    f"in {elapsed:.0f}s"
                ))

            except Exception as e:
                logger.error(f"Bulk insert failed: {e}")
                grid_run.status = UKRiskGridRun.Status.FAILED
                grid_run.error_message = str(e)
                grid_run.save()
                self.stderr.write(self.style.ERROR(f"  ✗ Storage failed: {e}"))
        else:
            grid_run.status = UKRiskGridRun.Status.FAILED
            grid_run.error_message = "No data fetched — all grid points failed"
            grid_run.save()
            self.stderr.write(self.style.ERROR("  ✗ No data fetched"))
