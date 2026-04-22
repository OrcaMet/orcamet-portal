"""
OrcaMet Portal — risk_grid management command (Multi-Model Ensemble)

Fetches weather data for a UK grid from ALL eligible models using BATCH
API calls to Open-Meteo, blends with geographic-aware weighting (matching
the site-specific forecast engine in core.py), and stores hourly ensemble
risk scores for the interactive contour map.

MEMORY-EFFICIENT: Processes one model at a time, accumulating weighted
sums into numpy arrays and discarding raw API data before fetching the
next model. Peak memory is O(1 model) not O(all models).

Usage:
    python manage.py risk_grid                     # Default 0.5° grid, all models
    python manage.py risk_grid --resolution 0.25   # Finer grid
    python manage.py risk_grid --days 2            # 2-day forecast
    python manage.py risk_grid --batch-size 30     # Smaller batches

After completion, run generate_contour_cache to pre-render map images:
    python manage.py generate_contour_cache
"""

import gc
import logging
import time
from datetime import datetime, timezone, timedelta

import numpy as np
import requests
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from forecasts.models import UKRiskGridRun, UKRiskGridPoint
from forecasts.engine.core import (
    calculate_hourly_risk,
    get_model_weights,
    is_in_domain,
    MODELS_CONFIG,
    OPENMETEO_HOST,
)

logger = logging.getLogger(__name__)

# ============================================================
# GRID CONFIGURATION
# ============================================================

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

HOURLY_VARS = "wind_speed_10m,wind_gusts_10m,precipitation,temperature_2m"

# Models to use for the grid (subset of MODELS_CONFIG to reduce
# API calls and memory on constrained environments like Render).
# UKV = best UK detail, ECMWF = longest horizon backbone,
# ICON-EU = Europe-wide, ARPEGE = synoptic backbone.
GRID_MODELS = ["ukv", "ecmwf", "icon_eu", "arpege_europe"]

# How many records to flush to DB at a time
DB_BATCH_SIZE = 5000

# Seconds to pause between API calls
BATCH_DELAY = 2.0
MODEL_DELAY = 5.0
RATE_LIMIT_WAIT = 60


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def _parse_timestamp(t_str):
    """Parse Open-Meteo timestamp string to timezone-aware datetime."""
    if "T" in t_str:
        cleaned = t_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    else:
        return datetime.strptime(t_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _safe_float(value, default=0.0):
    """Convert a value to float, returning default for None/NaN/Inf."""
    if value is None:
        return default
    try:
        f = float(value)
        if np.isnan(f) or np.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


def fetch_batch(model_name, lats, lons, start_date, end_date):
    """
    Fetch weather data for MULTIPLE locations in a single API call.
    Returns list of dicts, one per location. Failed locations return None.

    Raises requests.exceptions.HTTPError on non-retryable API errors.
    """
    config = MODELS_CONFIG[model_name]
    api_key = getattr(settings, "OPENMETEO_API_KEY", "")

    params = {
        "latitude": ",".join(f"{lat:.4f}" for lat in lats),
        "longitude": ",".join(f"{lon:.4f}" for lon in lons),
        "hourly": HOURLY_VARS,
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


# ============================================================
# MANAGEMENT COMMAND
# ============================================================

class Command(BaseCommand):
    help = "Generate UK-wide multi-model ensemble risk grid for the interactive contour map"

    def add_arguments(self, parser):
        parser.add_argument(
            "--resolution", type=float, default=0.5,
            help="Grid spacing in degrees (default: 0.5 ≈ 55km)",
        )
        parser.add_argument(
            "--days", type=int, default=3,
            help="Number of forecast days (default: 3)",
        )
        parser.add_argument(
            "--batch-size", type=int, default=50,
            help="Locations per API call (default: 50, max ~100)",
        )

    def handle(self, *args, **options):
        resolution = options["resolution"]
        num_days = options["days"]
        batch_size = options["batch_size"]
        grid_run = None

        try:
            grid_run = self._run_pipeline(resolution, num_days, batch_size)
        except CommandError:
            # CommandError already sets grid_run status if applicable
            raise
        except Exception as e:
            # Catch any unexpected error, mark the run as failed, then re-raise
            # so Render sees a non-zero exit code
            logger.exception("Unexpected error in risk_grid")
            if grid_run:
                grid_run.status = UKRiskGridRun.Status.FAILED
                grid_run.error_message = f"Unexpected error: {e}"
                grid_run.save()
            raise CommandError(f"Unexpected error: {e}")

    def _run_pipeline(self, resolution, num_days, batch_size):
        """
        Main pipeline. Returns the UKRiskGridRun on success.
        Raises CommandError on any failure.
        """

        # ==============================================================
        # STARTUP VALIDATION
        # ==============================================================
        api_key = getattr(settings, "OPENMETEO_API_KEY", "")
        self.stdout.write(f"  Open-Meteo host: {OPENMETEO_HOST}")
        if not api_key:
            self.stdout.write(self.style.WARNING(
                "  ⚠ OPENMETEO_API_KEY is not set — using unauthenticated access. "
                "This WILL be rate-limited on a grid workload."
            ))
        else:
            self.stdout.write(f"  API key: {'*' * 4}{api_key[-4:]}")

        # ==============================================================
        # BUILD THE GRID
        # ==============================================================
        lats = np.arange(UK_LAT_MIN, UK_LAT_MAX + resolution, resolution)
        lons = np.arange(UK_LON_MIN, UK_LON_MAX + resolution, resolution)
        grid_points = [
            (round(float(lat), 4), round(float(lon), 4))
            for lat in lats for lon in lons
        ]
        total_points = len(grid_points)

        today = datetime.now(timezone.utc).date()
        end_date = today + timedelta(days=num_days - 1)
        start_str = today.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")

        # Fast lookup: (lat, lon) -> index in grid_points
        point_index = {pt: i for i, pt in enumerate(grid_points)}

        # Determine which models cover which grid points
        model_points = {}
        for model_name in GRID_MODELS:
            in_domain = [
                pt for pt in grid_points
                if is_in_domain(model_name, pt[0], pt[1])
            ]
            if in_domain:
                model_points[model_name] = in_domain

        if not model_points:
            raise CommandError(
                "No models have coverage for any grid points. "
                "Check GRID_MODELS and DOMAIN_BOUNDS in core.py."
            )

        total_api_calls = sum(
            (len(pts) + batch_size - 1) // batch_size
            for pts in model_points.values()
        )

        self.stdout.write(
            f"\nGenerating UK ensemble risk grid: {len(lats)}×{len(lons)} = "
            f"{total_points} points at {resolution}° resolution"
        )
        self.stdout.write(f"  Period: {today} to {end_date} ({num_days} days)")
        self.stdout.write(f"  Models: {len(model_points)} eligible")
        for m, pts in model_points.items():
            n_b = (len(pts) + batch_size - 1) // batch_size
            self.stdout.write(
                f"    {MODELS_CONFIG[m]['name']}: {len(pts)} pts → {n_b} calls"
            )
        self.stdout.write(
            f"  Total API calls: {total_api_calls} "
            f"(vs {total_points * len(model_points)} individual)"
        )

        # ==============================================================
        # PRE-COMPUTE GEOGRAPHIC WEIGHTS
        # ==============================================================
        self.stdout.write("  Pre-computing geographic weights...")
        weight_table = []
        for lat, lon in grid_points:
            w = get_model_weights(lat, lon, exposure="urban")
            weight_table.append(w)

        # ==============================================================
        # CREATE THE RUN RECORD
        # ==============================================================
        models_used = list(model_points.keys())
        grid_run = UKRiskGridRun.objects.create(
            forecast_date=today,
            status=UKRiskGridRun.Status.RUNNING,
            lat_min=UK_LAT_MIN,
            lat_max=UK_LAT_MAX,
            lon_min=UK_LON_MIN,
            lon_max=UK_LON_MAX,
            resolution=resolution,
            grid_points=total_points,
            models_used=models_used,
        )

        start_time = time.time()

        # ==============================================================
        # PHASE 1: PROBE FOR TIMESTAMP AXIS
        # ==============================================================
        probe_model = "ecmwf" if "ecmwf" in model_points else next(iter(model_points))
        probe_pts = model_points[probe_model][:1]
        self.stdout.write(
            f"\n  Probing {MODELS_CONFIG[probe_model]['name']} for timestamp axis..."
        )

        try:
            probe_result = fetch_batch(
                probe_model,
                [probe_pts[0][0]], [probe_pts[0][1]],
                start_str, end_str,
            )
            time.sleep(1.0)
        except Exception as e:
            grid_run.status = UKRiskGridRun.Status.FAILED
            grid_run.error_message = f"Probe failed: {e}"
            grid_run.save()
            raise CommandError(f"Probe API call failed: {e}")

        if not probe_result or probe_result[0] is None:
            grid_run.status = UKRiskGridRun.Status.FAILED
            grid_run.error_message = "Probe returned no data"
            grid_run.save()
            raise CommandError(
                "Probe returned no data. Check API key and endpoint. "
                f"Model: {probe_model}, URL: {MODELS_CONFIG[probe_model]['url']}"
            )

        ref_times = probe_result[0]["time"]
        num_hours = len(ref_times)
        self.stdout.write(f"  Timestamp axis: {num_hours} hours")

        # ==============================================================
        # PHASE 2: ALLOCATE NUMPY ACCUMULATORS
        # ==============================================================
        acc_wind = np.zeros((total_points, num_hours), dtype=np.float32)
        acc_gust = np.zeros((total_points, num_hours), dtype=np.float32)
        acc_prcp = np.zeros((total_points, num_hours), dtype=np.float32)
        acc_temp = np.zeros((total_points, num_hours), dtype=np.float32)
        acc_wt = np.zeros((total_points, num_hours), dtype=np.float32)

        mem_mb = total_points * num_hours * 5 * 4 / 1024 / 1024
        self.stdout.write(
            f"  Accumulators: {total_points}×{num_hours} = {mem_mb:.1f} MB"
        )

        # ==============================================================
        # PHASE 3: FETCH EACH MODEL, ACCUMULATE, DISCARD
        # ==============================================================
        api_calls_made = 0
        successful_models = []
        total_pts_ok = 0
        total_pts_fail = 0

        for model_name, points in model_points.items():
            model_display = MODELS_CONFIG[model_name]["name"]
            n_batches = (len(points) + batch_size - 1) // batch_size
            self.stdout.write(
                f"\n  Fetching {model_display} "
                f"({len(points)} pts, {n_batches} batches)..."
            )

            model_pts_ok = 0
            model_pts_fail = 0

            for batch_idx in range(n_batches):
                b_start = batch_idx * batch_size
                b_end = min(b_start + batch_size, len(points))
                batch_pts = points[b_start:b_end]
                batch_lats = [p[0] for p in batch_pts]
                batch_lons = [p[1] for p in batch_pts]

                results = None
                try:
                    results = fetch_batch(
                        model_name, batch_lats, batch_lons,
                        start_str, end_str,
                    )
                    api_calls_made += 1
                except requests.exceptions.HTTPError as e:
                    if e.response is not None and e.response.status_code == 429:
                        self.stdout.write(self.style.WARNING(
                            f"    ⚠ Rate limited on batch {batch_idx + 1}/{n_batches}! "
                            f"Waiting {RATE_LIMIT_WAIT}s..."
                        ))
                        time.sleep(RATE_LIMIT_WAIT)
                        try:
                            results = fetch_batch(
                                model_name, batch_lats, batch_lons,
                                start_str, end_str,
                            )
                            api_calls_made += 1
                        except Exception as retry_e:
                            self.stdout.write(self.style.ERROR(
                                f"    ✗ Retry failed: {retry_e}"
                            ))
                    else:
                        self.stdout.write(self.style.ERROR(
                            f"    ✗ Batch {batch_idx + 1}/{n_batches} HTTP error: {e}"
                        ))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(
                        f"    ✗ Batch {batch_idx + 1}/{n_batches} failed: {e}"
                    ))

                # Accumulate results into numpy arrays
                if results:
                    for result in results:
                        if result is None:
                            model_pts_fail += 1
                            continue

                        key = (result["lat"], result["lon"])
                        idx = point_index.get(key)
                        if idx is None:
                            model_pts_fail += 1
                            continue

                        w = weight_table[idx].get(model_name, 0.0)
                        if w <= 0:
                            continue

                        ws = result.get("wind_speed", [])
                        gs = result.get("wind_gusts", [])
                        ps = result.get("precipitation", [])
                        ts = result.get("temperature", [])
                        n_t = min(len(ws), len(gs), len(ps), len(ts), num_hours)

                        for t in range(n_t):
                            wv = _safe_float(ws[t] if t < len(ws) else None)
                            gv = _safe_float(gs[t] if t < len(gs) else None)
                            pv = _safe_float(ps[t] if t < len(ps) else None)
                            tv = _safe_float(ts[t] if t < len(ts) else None, 10.0)

                            acc_wind[idx, t] += w * wv
                            acc_gust[idx, t] += w * gv
                            acc_prcp[idx, t] += w * pv
                            acc_temp[idx, t] += w * tv
                            acc_wt[idx, t] += w

                        model_pts_ok += 1
                else:
                    model_pts_fail += len(batch_pts)

                # Progress logging every 5 batches
                if (batch_idx + 1) % 5 == 0 or batch_idx == n_batches - 1:
                    self.stdout.write(
                        f"    Batch {batch_idx + 1}/{n_batches} complete "
                        f"({model_pts_ok} OK, {model_pts_fail} failed)"
                    )

                # Pause between batches to respect rate limits
                time.sleep(BATCH_DELAY)

            # Log model result
            total_pts_ok += model_pts_ok
            total_pts_fail += model_pts_fail

            if model_pts_ok > 0:
                successful_models.append(model_name)
                self.stdout.write(self.style.SUCCESS(
                    f"  ✓ {model_display}: {model_pts_ok} pts OK"
                    + (f", {model_pts_fail} failed" if model_pts_fail else "")
                ))
            else:
                self.stdout.write(self.style.ERROR(
                    f"  ✗ {model_display}: ALL {model_pts_fail} points failed"
                ))

            # Free memory after each model
            gc.collect()

            # Pause between models
            time.sleep(MODEL_DELAY)

        # Check we got at least some data
        if not successful_models:
            grid_run.status = UKRiskGridRun.Status.FAILED
            grid_run.error_message = (
                f"All {len(model_points)} models failed. "
                f"API calls made: {api_calls_made}. "
                f"Check OPENMETEO_API_KEY and network connectivity."
            )
            grid_run.save()
            raise CommandError(
                f"All models failed after {api_calls_made} API calls. "
                f"Host: {OPENMETEO_HOST}, "
                f"API key set: {bool(getattr(settings, 'OPENMETEO_API_KEY', ''))}"
            )

        # ==============================================================
        # PHASE 4: NORMALISE AND COMPUTE RISK SCORES
        # ==============================================================
        self.stdout.write(
            f"\n  Normalising ensemble from {len(successful_models)} models "
            f"and computing risk scores..."
        )

        # Normalise: divide accumulated sums by total weight
        mask = acc_wt > 0
        acc_wind = np.where(mask, acc_wind / acc_wt, 0.0)
        acc_gust = np.where(mask, acc_gust / acc_wt, 0.0)
        acc_prcp = np.where(mask, acc_prcp / acc_wt, 0.0)
        acc_temp = np.where(mask, acc_temp / acc_wt, 10.0)

        # Free weight accumulator
        del acc_wt
        gc.collect()

        # Build DB records in chunks
        all_point_records = []
        blend_errors = 0
        records_flushed = 0

        for pt_idx, (lat, lon) in enumerate(grid_points):
            if not mask[pt_idx].any():
                blend_errors += 1
                continue

            for t_idx in range(num_hours):
                if not mask[pt_idx, t_idx]:
                    continue

                w = float(acc_wind[pt_idx, t_idx])
                g = float(acc_gust[pt_idx, t_idx])
                p = float(acc_prcp[pt_idx, t_idx])
                t = float(acc_temp[pt_idx, t_idx])

                risk = calculate_hourly_risk(w, g, p, t, DEFAULT_THRESHOLDS)

                all_point_records.append(UKRiskGridPoint(
                    run=grid_run,
                    latitude=lat,
                    longitude=lon,
                    timestamp=_parse_timestamp(ref_times[t_idx]),
                    wind_speed=round(w, 2),
                    wind_gusts=round(g, 2),
                    precipitation=round(p, 2),
                    temperature=round(t, 2),
                    risk=round(risk, 2),
                ))

                # Flush to DB periodically to limit in-memory records
                if len(all_point_records) >= DB_BATCH_SIZE:
                    UKRiskGridPoint.objects.bulk_create(all_point_records)
                    records_flushed += len(all_point_records)
                    self.stdout.write(
                        f"    Flushed {records_flushed} records to DB "
                        f"({pt_idx + 1}/{total_points} points processed)"
                    )
                    all_point_records = []

        # Flush remaining
        if all_point_records:
            UKRiskGridPoint.objects.bulk_create(all_point_records)
            records_flushed += len(all_point_records)

        # Free numpy arrays
        del acc_wind, acc_gust, acc_prcp, acc_temp, mask
        gc.collect()

        # ==============================================================
        # PHASE 5: FINALISE
        # ==============================================================
        successful_points = total_points - blend_errors
        total_records = UKRiskGridPoint.objects.filter(run=grid_run).count()

        if total_records == 0:
            grid_run.status = UKRiskGridRun.Status.FAILED
            grid_run.error_message = (
                f"No data produced after blending. "
                f"Models OK: {successful_models}, "
                f"blend_errors: {blend_errors}/{total_points}"
            )
            grid_run.save()
            raise CommandError(
                f"No data produced after blending {len(successful_models)} models. "
                f"{blend_errors}/{total_points} points had zero weight."
            )

        # Mark run as successful
        grid_run.status = UKRiskGridRun.Status.SUCCESS
        grid_run.num_hours = num_hours
        grid_run.models_used = successful_models
        grid_run.save()

        elapsed = time.time() - start_time

        # Clean up old runs AFTER the new one is confirmed successful
        old_runs = UKRiskGridRun.objects.filter(
            forecast_date=today,
            status=UKRiskGridRun.Status.SUCCESS,
        ).exclude(pk=grid_run.pk)
        old_count = old_runs.count()
        if old_count > 0:
            old_runs.delete()
            self.stdout.write(f"  Cleaned up {old_count} previous run(s) for today")

        # Also clean up old failed runs (keep last 5 for debugging)
        failed_runs = UKRiskGridRun.objects.filter(
            status=UKRiskGridRun.Status.FAILED,
        ).order_by("-forecast_date")
        if failed_runs.count() > 5:
            stale_failed = failed_runs[5:]
            stale_ids = list(stale_failed.values_list("pk", flat=True))
            UKRiskGridRun.objects.filter(pk__in=stale_ids).delete()

        self.stdout.write(self.style.SUCCESS(
            f"\n  ✓ Complete: {total_records} records "
            f"({successful_points} points, {blend_errors} skipped) "
            f"in {elapsed:.0f}s using {api_calls_made} API calls "
            f"across {len(successful_models)} models"
        ))
        self.stdout.write(
            f"  Models: "
            f"{', '.join(MODELS_CONFIG[m]['name'] for m in successful_models)}"
        )

        return grid_run
