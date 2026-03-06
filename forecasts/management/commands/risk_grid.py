"""
OrcaMet Portal — risk_grid management command

Fetches weather data for a grid of points across the UK using BATCH
API calls to Open-Meteo (up to 50 locations per request), computes
hourly risk scores at each point, and stores the results for the
interactive contour map.

Usage:
    python manage.py risk_grid                     # Default 0.5° grid
    python manage.py risk_grid --resolution 0.25   # Finer grid
    python manage.py risk_grid --days 2            # 2-day forecast
    python manage.py risk_grid --batch-size 30     # Smaller batches
"""

import logging
import time
from datetime import datetime, timezone, timedelta

import numpy as np
import requests
from django.core.management.base import BaseCommand
from django.conf import settings

from forecasts.models import UKRiskGridRun, UKRiskGridPoint
from forecasts.engine.core import (
    calculate_hourly_risk,
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


def _parse_timestamp(t_str):
    """Parse Open-Meteo timestamp string to timezone-aware datetime."""
    if "T" in t_str:
        # e.g. "2026-03-03T00:00" or "2026-03-03T00:00:00Z"
        cleaned = t_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    else:
        # e.g. "2026-03-03"
        return datetime.strptime(t_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def fetch_batch(model_name, lats, lons, start_date, end_date):
    """
    Fetch weather data for MULTIPLE locations in a single API call.

    Open-Meteo accepts comma-separated latitude/longitude values and
    returns an array of results (one per location).

    Args:
        model_name: Key from MODELS_CONFIG (e.g. 'ecmwf')
        lats: List of latitude floats
        lons: List of longitude floats
        start_date: Start date string 'YYYY-MM-DD'
        end_date: End date string 'YYYY-MM-DD'

    Returns:
        List of dicts, one per location, each containing:
            {lat, lon, time, wind_speed, wind_gusts, precipitation, temperature}
        Failed locations return None in the list.
    """
    config = MODELS_CONFIG[model_name]
    api_key = getattr(settings, "OPENMETEO_API_KEY", "")

    params = {
        "latitude": ",".join(f"{lat:.4f}" for lat in lats),
        "longitude": ",".join(f"{lon:.4f}" for lon in lons),
        "hourly": "wind_speed_10m,wind_gusts_10m,precipitation,temperature_2m",
        "timezone": "UTC",
        "wind_speed_unit": "ms",
        "precipitation_unit": "mm",
        "start_date": start_date,
        "end_date": end_date,
        **config["params"],
    }
    if api_key:
        params["apikey"] = api_key

    resp = requests.get(config["url"], params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    # Single location returns a dict; multiple returns a list
    if isinstance(data, dict):
        data = [data]

    results = []
    for i, item in enumerate(data):
        h = item.get("hourly", {})
        if not h or "time" not in h:
            results.append(None)
            continue
        results.append({
            "lat": lats[i],
            "lon": lons[i],
            "time": h["time"],
            "wind_speed": h.get("wind_speed_10m", []),
            "wind_gusts": h.get("wind_gusts_10m", []),
            "precipitation": h.get("precipitation", []),
            "temperature": h.get("temperature_2m", []),
        })

    return results


class Command(BaseCommand):
    help = "Generate UK-wide risk grid for the interactive contour map"

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
            help="Model to use (default: ecmwf). "
                 "Options: " + ", ".join(MODELS_CONFIG.keys()),
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=50,
            help="Locations per API call (default: 50, max ~100)",
        )

    def handle(self, *args, **options):
        resolution = options["resolution"]
        num_days = options["days"]
        model_name = options["model"]
        batch_size = options["batch_size"]

        if model_name not in MODELS_CONFIG:
            self.stderr.write(
                f"Unknown model '{model_name}'. "
                f"Available: {', '.join(MODELS_CONFIG.keys())}"
            )
            return

        # Build the grid
        lats = np.arange(UK_LAT_MIN, UK_LAT_MAX + resolution, resolution)
        lons = np.arange(UK_LON_MIN, UK_LON_MAX + resolution, resolution)
        grid_points = [(round(float(lat), 4), round(float(lon), 4))
                       for lat in lats for lon in lons]
        total_points = len(grid_points)

        today = datetime.now(timezone.utc).date()
        end_date = today + timedelta(days=num_days - 1)
        start_str = today.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")

        # Calculate number of API calls needed
        num_batches = (total_points + batch_size - 1) // batch_size

        self.stdout.write(
            f"Generating UK risk grid: {len(lats)}×{len(lons)} = "
            f"{total_points} points at {resolution}° resolution"
        )
        self.stdout.write(f"  Model: {MODELS_CONFIG[model_name]['name']}")
        self.stdout.write(f"  Period: {today} to {end_date} ({num_days} days)")
        self.stdout.write(
            f"  Batch size: {batch_size} → {num_batches} API calls "
            f"(instead of {total_points})"
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
        processed_points = 0
        start_time = time.time()

        # Process in batches
        for batch_idx in range(num_batches):
            batch_start = batch_idx * batch_size
            batch_end = min(batch_start + batch_size, total_points)
            batch_points = grid_points[batch_start:batch_end]

            batch_lats = [p[0] for p in batch_points]
            batch_lons = [p[1] for p in batch_points]

            elapsed = time.time() - start_time
            rate = processed_points / elapsed if elapsed > 0 else 0
            remaining = total_points - processed_points
            eta = remaining / rate if rate > 0 else 0

            self.stdout.write(
                f"  Batch {batch_idx + 1}/{num_batches} "
                f"({len(batch_points)} pts) — "
                f"{processed_points}/{total_points} done, "
                f"{rate:.1f} pts/s, ETA {eta:.0f}s"
            )

            try:
                results = fetch_batch(
                    model_name, batch_lats, batch_lons, start_str, end_str
                )

                for result in results:
                    if result is None:
                        failed_points += 1
                        processed_points += 1
                        continue

                    lat = result["lat"]
                    lon = result["lon"]
                    times = result["time"]
                    winds = result.get("wind_speed", [])
                    gusts = result.get("wind_gusts", [])
                    precips = result.get("precipitation", [])
                    temps = result.get("temperature", [])

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
                            timestamp=_parse_timestamp(t_str),
                            wind_speed=round(w, 2),
                            wind_gusts=round(g, 2),
                            precipitation=round(p, 2),
                            temperature=round(temp, 2),
                            risk=round(risk, 2),
                        ))

                    processed_points += 1

            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    # Rate limited — wait and retry
                    wait_time = 30
                    self.stdout.write(
                        self.style.WARNING(
                            f"  ⚠ Rate limited! Waiting {wait_time}s before retry..."
                        )
                    )
                    time.sleep(wait_time)

                    # Retry this batch
                    try:
                        results = fetch_batch(
                            model_name, batch_lats, batch_lons, start_str, end_str
                        )
                        for result in results:
                            if result is None:
                                failed_points += 1
                                processed_points += 1
                                continue

                            lat = result["lat"]
                            lon = result["lon"]
                            times = result["time"]
                            winds = result.get("wind_speed", [])
                            gusts = result.get("wind_gusts", [])
                            precips = result.get("precipitation", [])
                            temps = result.get("temperature", [])

                            for i, t_str in enumerate(times):
                                w = float(winds[i]) if i < len(winds) and winds[i] is not None else 0.0
                                g = float(gusts[i]) if i < len(gusts) and gusts[i] is not None else 0.0
                                p = float(precips[i]) if i < len(precips) and precips[i] is not None else 0.0
                                temp = float(temps[i]) if i < len(temps) and temps[i] is not None else 10.0

                                w = 0.0 if (np.isnan(w) or np.isinf(w)) else w
                                g = 0.0 if (np.isnan(g) or np.isinf(g)) else g
                                p = 0.0 if (np.isnan(p) or np.isinf(p)) else p
                                temp = 10.0 if (np.isnan(temp) or np.isinf(temp)) else temp

                                risk = calculate_hourly_risk(w, g, p, temp, DEFAULT_THRESHOLDS)

                                all_point_records.append(UKRiskGridPoint(
                                    run=grid_run,
                                    latitude=lat,
                                    longitude=lon,
                                    timestamp=_parse_timestamp(t_str),
                                    wind_speed=round(w, 2),
                                    wind_gusts=round(g, 2),
                                    precipitation=round(p, 2),
                                    temperature=round(temp, 2),
                                    risk=round(risk, 2),
                                ))

                            processed_points += 1

                    except Exception as retry_e:
                        self.stdout.write(
                            self.style.ERROR(f"  ✗ Retry failed: {retry_e}")
                        )
                        failed_points += len(batch_points)
                        processed_points += len(batch_points)
                else:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  ✗ Batch {batch_idx + 1} failed: {e}"
                        )
                    )
                    failed_points += len(batch_points)
                    processed_points += len(batch_points)

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f"  ✗ Batch {batch_idx + 1} failed: {e}"
                    )
                )
                failed_points += len(batch_points)
                processed_points += len(batch_points)

            # Pause between batches — be polite to the API
            time.sleep(1.0)

        # Bulk insert all points
        if all_point_records:
            self.stdout.write(
                f"  Storing {len(all_point_records)} grid point records..."
            )
            try:
                batch_db_size = 5000
                for i in range(0, len(all_point_records), batch_db_size):
                    batch = all_point_records[i:i + batch_db_size]
                    UKRiskGridPoint.objects.bulk_create(batch)

                grid_run.status = UKRiskGridRun.Status.SUCCESS
                successful = total_points - failed_points
                grid_run.num_hours = (
                    len(all_point_records) // max(successful, 1)
                )
                grid_run.save()

                elapsed = time.time() - start_time
                self.stdout.write(self.style.SUCCESS(
                    f"  ✓ Complete: {len(all_point_records)} records "
                    f"({successful} points, {failed_points} failed) "
                    f"in {elapsed:.0f}s using {num_batches} API calls"
                ))

            except Exception as e:
                logger.error(f"Bulk insert failed: {e}")
                grid_run.status = UKRiskGridRun.Status.FAILED
                grid_run.error_message = str(e)
                grid_run.save()
                self.stderr.write(
                    self.style.ERROR(f"  ✗ Storage failed: {e}")
                )
        else:
            grid_run.status = UKRiskGridRun.Status.FAILED
            grid_run.error_message = "No data fetched — all grid points failed"
            grid_run.save()
            self.stderr.write(self.style.ERROR("  ✗ No data fetched"))
