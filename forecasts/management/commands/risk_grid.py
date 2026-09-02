"""
OrcaMet Portal — risk_grid management command (ensemble)

Builds the UK contour map from an ensemble rather than a blend of
deterministic models, so the headline layer can be a chance of
cancellation rather than a severity index.

For each grid point it fetches ECMWF's 51 members, counts how many breach
any cancel limit at each hour, and stores that share alongside the member
mean of each weather variable.

MEMORY-EFFICIENT: one batch of points at a time, accumulated into numpy
arrays and discarded before the next. Peak memory is O(1 batch), not
O(all members).

Usage:
    python manage.py risk_grid                     # Default 0.5° grid
    python manage.py risk_grid --resolution 0.25   # Finer grid
    python manage.py risk_grid --days 2            # 2-day forecast
    python manage.py risk_grid --batch-size 5      # Gentler on rate limits

This command also pre-renders the contour overlays the interactive map
serves from /dashboard/map/contour.png (see --contour-vars).
"""

import gc
import logging
import time
from datetime import datetime, timezone, timedelta

import numpy as np
import requests  # for requests.exceptions.HTTPError in the retry path
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.utils import timezone as dj_timezone

from forecasts.models import (
    UKRiskGridRun, UKRiskGridPoint, CachedContourImage, MapThresholds,
)
from forecasts.engine.core import (
    calculate_hourly_risk,
    MODELS_CONFIG,
    scrub_key,
    _session,
)
from forecasts.engine import ensemble as ens

logger = logging.getLogger(__name__)

# ============================================================
# GRID CONFIGURATION
# ============================================================

# UK bounding box (covers mainland GB + Northern Ireland)
UK_LAT_MIN = 49.9
UK_LAT_MAX = 58.7
UK_LON_MIN = -7.6
UK_LON_MAX = 1.8

# Thresholds for the grid are generic — the map is UK-wide, so there is no
# site-specific exposure to draw on. They now live in the MapThresholds
# singleton, editable in the Django admin; the starting values are that
# model's field defaults.

HOURLY_VARS = "wind_speed_10m,wind_gusts_10m,precipitation,temperature_2m"

# How many records to flush to DB at a time
DB_BATCH_SIZE = 5000

# Seconds to pause between API calls.
#
# Ensemble payloads trip Open-Meteo's minutely limit long before its daily
# one: a single 8-point ECMWF call carries 51 members across 4 variables, and
# measured against the live API only about four such calls a minute are
# sustainable. At 0.5 degrees the full grid is ~48 calls, so this pacing puts
# a run at roughly 12 minutes — comfortable inside a 6-hourly cron, and far
# better than a fast run that 429s away a third of the country.
BATCH_DELAY = 15.0
RATE_LIMIT_WAIT = 75

# Pacing adapts during a run. A fixed delay cannot recover once the limiter
# bites: every later batch hits it too, exhausts its single retry and is
# dropped. A live run lost every batch north of 55.9N that way and still
# reported success, leaving Scotland missing from the map.
BATCH_DELAY_MAX = 60.0
BACKOFF_FACTOR = 1.5

# Batches that are still rate limited after their retry are set aside and
# swept once more at the end, after a cooldown, rather than left as a hole.
RETRY_PASS_COOLDOWN = 120


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


def _is_rate_limited(exc):
    """
    Did this failure come from Open-Meteo's rate limiter?

    The shared session already retries 429 with backoff, then gives up and
    raises a ConnectionError wrapping urllib3's MaxRetryError — so checking
    for HTTPError alone misses every exhausted-retry case, which is exactly
    the one worth waiting out.
    """
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    return "429" in str(exc)


def _safe_float(value):
    """
    Convert a value to float, returning NaN for None/NaN/Inf/garbage.

    This deliberately does NOT substitute a benign default. Earlier versions
    returned 0.0 for wind/gust/precipitation and 10.0 for temperature, so a
    gap in the API feed was blended in as "calm, dry and mild" — the lowest
    risk values possible. Missing data now propagates as NaN and is excluded
    from the ensemble entirely.
    """
    if value is None:
        return np.nan
    try:
        f = float(value)
    except (ValueError, TypeError):
        return np.nan
    if np.isnan(f) or np.isinf(f):
        return np.nan
    return f


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

    # Use the shared session so grid fetches get the same retry/backoff on
    # 429/5xx that single-site fetches do. This is the heaviest API workload
    # in the app and was previously the only one without it.
    resp = _session.get(config["url"], params=params, timeout=60)
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
    help = "Generate the UK-wide ensemble grid behind the interactive contour map"

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
            "--batch-size", type=int, default=10,
            help=(
                "Locations per API call (default: 10). Ensemble payloads are "
                "large — 25 points of ECMWF is ~2 MB — and Open-Meteo's "
                "minutely limit is reached well before the bandwidth is."
            ),
        )
        parser.add_argument(
            "--contour-vars", type=str, default="pcancel,wind,gust,precip,temp",
            help=(
                "Comma-separated variables to pre-render contour overlays for "
                "(default: pcancel,wind,gust,precip,temp — all map tabs). Use "
                "'none' to skip rendering. Each variable adds one PNG per "
                "forecast hour to the database."
            ),
        )
        parser.add_argument(
            "--retention-days", type=int, default=2,
            help="Delete grid runs with a forecast_date older than this (default: 2)",
        )

    def handle(self, *args, **options):
        resolution = options["resolution"]
        num_days = options["days"]
        batch_size = options["batch_size"]
        retention_days = options["retention_days"]

        raw_vars = (options["contour_vars"] or "").strip().lower()
        if raw_vars in ("", "none"):
            contour_vars = []
        else:
            contour_vars = [v.strip() for v in raw_vars.split(",") if v.strip()]

        valid_vars = set(CachedContourImage.Variable.values)
        unknown = [v for v in contour_vars if v not in valid_vars]
        if unknown:
            raise CommandError(
                f"Unknown --contour-vars value(s): {', '.join(unknown)}. "
                f"Valid options: {', '.join(sorted(valid_vars))}"
            )

        grid_run = None

        try:
            grid_run = self._run_pipeline(
                resolution, num_days, batch_size, contour_vars, retention_days
            )
        except CommandError:
            # CommandError already sets grid_run status if applicable
            raise
        except Exception as e:
            # Catch any unexpected error, mark the run as failed, then re-raise
            # so Render sees a non-zero exit code
            logger.exception("Unexpected error in risk_grid")
            if grid_run:
                grid_run.status = UKRiskGridRun.Status.FAILED
                grid_run.error_message = f"Unexpected error: {scrub_key(e)}"
                grid_run.save()
            raise CommandError(f"Unexpected error: {scrub_key(e)}")

    def _render_contours(self, grid_run, contour_vars):
        """
        Pre-render contour overlay PNGs for the completed grid run.

        The map serves these from /dashboard/map/contour.png. Rendering here
        (in the cron job) rather than in the web request keeps matplotlib and
        scipy out of the gunicorn workers, which are memory-constrained.
        """
        # Imported lazily so the fetch phase does not pay the matplotlib
        # import cost, and so a missing optional dependency cannot stop a
        # grid run that has already succeeded.
        from forecasts.engine.map_interpolation import render_contour_to_bytes

        field_for_var = {
            "pcancel": "p_cancel",
            "risk": "risk",
            "wind": "wind_speed",
            "gust": "wind_gusts",
            "precip": "precipitation",
            "temp": "temperature",
        }

        timestamps = list(
            UKRiskGridPoint.objects
            .filter(run=grid_run)
            .values_list("timestamp", flat=True)
            .distinct()
            .order_by("timestamp")
        )

        self.stdout.write(
            f"\n  Rendering contours for {', '.join(contour_vars)} "
            f"across {len(timestamps)} hour(s)..."
        )

        rendered = 0
        failed = 0

        for ts in timestamps:
            rows = list(
                UKRiskGridPoint.objects
                .filter(run=grid_run, timestamp=ts)
                .values_list(
                    "latitude", "longitude", "risk",
                    "wind_speed", "wind_gusts", "precipitation", "temperature",
                    "p_cancel",
                )
            )
            if len(rows) < 4:
                # interpolate_risk_surface needs at least 4 points
                continue

            # None -> NaN, so a point with no ensemble members is
            # excluded from the surface rather than drawn as 0%.
            arr = np.array(
                [[np.nan if v is None else v for v in row] for row in rows],
                dtype=float,
            )
            lats = arr[:, 0]
            lons = arr[:, 1]
            columns = {
                "risk": arr[:, 2],
                "wind_speed": arr[:, 3],
                "wind_gusts": arr[:, 4],
                "precipitation": arr[:, 5],
                "temperature": arr[:, 6],
                "p_cancel": arr[:, 7],
            }

            images = []
            for var in contour_vars:
                values = columns[field_for_var[var]]

                # Drop points with no value for this variable; the surface is
                # interpolated from what is known, not from invented zeroes.
                usable = np.isfinite(values)
                if usable.sum() < 4:
                    continue
                try:
                    png = render_contour_to_bytes(
                        lats[usable], lons[usable], values[usable], variable=var
                    )
                except Exception as e:
                    failed += 1
                    logger.warning(
                        f"Contour render failed ({var} @ {ts}): {scrub_key(e)}"
                    )
                    continue

                images.append(CachedContourImage(
                    run=grid_run,
                    timestamp=ts,
                    variable=var,
                    image_data=png,
                ))

            if images:
                # ignore_conflicts guards the (run, timestamp, variable)
                # unique constraint if this command is re-run for a run.
                CachedContourImage.objects.bulk_create(
                    images, ignore_conflicts=True
                )
                rendered += len(images)

            gc.collect()

        if failed:
            self.stdout.write(self.style.WARNING(
                f"  Contours: {rendered} rendered, {failed} failed"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"  Contours: {rendered} image(s) cached"
            ))

    def _run_pipeline(self, resolution, num_days, batch_size,
                      contour_vars, retention_days):
        """
        Main pipeline. Returns the UKRiskGridRun on success.
        Raises CommandError on any failure.
        """

        # ==============================================================
        # STARTUP VALIDATION
        # ==============================================================
        api_key = getattr(settings, "OPENMETEO_API_KEY", "")
        # Report the endpoint the ensemble will actually call. Printing the
        # base host while the grid quietly used a different one is what let
        # an unused API key go unnoticed.
        self.stdout.write(f"  Open-Meteo host: {ens.ensemble_url()}")
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

        # Local date, matching the per-site runner and the retention window
        # in cleanup_forecasts. Both compare against forecast_date.
        today = dj_timezone.localdate()
        end_date = today + timedelta(days=num_days - 1)
        start_str = today.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")

        # Fast lookup: (lat, lon) -> index in grid_points
        point_index = {pt: i for i, pt in enumerate(grid_points)}

        # Domain filtering is gone with the deterministic models: the grid is
        # now built from one global ensemble, which covers every UK point.
        total_api_calls = (total_points + batch_size - 1) // batch_size

        self.stdout.write(
            f"\nGenerating UK ensemble risk grid: {len(lats)}×{len(lons)} = "
            f"{total_points} points at {resolution}° resolution"
        )
        self.stdout.write(f"  Period: {today} to {end_date} ({num_days} days)")
        self.stdout.write(f"  Ensemble: {ens.GRID_ENSEMBLE}")
        self.stdout.write(
            f"  Total API calls: {total_api_calls} "
            f"({batch_size} points per call)"
        )

        # ==============================================================
        # RISK THRESHOLDS
        # ==============================================================
        # Read once per run, not per grid point: the values must not change
        # part-way through, or one map would be scored two different ways.
        thresholds = MapThresholds.load().as_dict()
        self.stdout.write(
            "  Risk thresholds: "
            f"wind {thresholds['wind_mean_caution']}/{thresholds['wind_mean_cancel']}, "
            f"gust {thresholds['gust_caution']}/{thresholds['gust_cancel']}, "
            f"precip {thresholds['precip_caution']}/{thresholds['precip_cancel']}, "
            f"cold {thresholds['temp_min_caution']}/{thresholds['temp_min_cancel']}, "
            + (
                f"heat {thresholds['temp_max_caution']}/{thresholds['temp_max_cancel']} "
                if thresholds.get("temp_max_caution") is not None
                else "heat off "
            )
            + "(caution/cancel — edit in Django admin)"
        )

        # Geographic model weighting is gone with the deterministic blend.
        # It existed to lean on UKV's 2 km detail, which a 0.5° grid (~55 km
        # cells) discards in the interpolation regardless — so nothing real
        # is lost, and every member now counts equally.

        # ==============================================================
        # CREATE THE RUN RECORD
        # ==============================================================
        models_used = [ens.GRID_ENSEMBLE]
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
        self.stdout.write(
            f"\n  Probing {ens.GRID_ENSEMBLE} for timestamp axis..."
        )

        try:
            ref_times, probe_points = ens.fetch_grid_members(
                [grid_points[0][0]], [grid_points[0][1]],
                forecast_days=num_days,
            )
            time.sleep(1.0)
        except Exception as e:
            grid_run.status = UKRiskGridRun.Status.FAILED
            grid_run.error_message = f"Probe failed: {scrub_key(e)}"
            grid_run.save()
            raise CommandError(f"Ensemble probe failed: {scrub_key(e)}")

        if not ref_times or not probe_points or probe_points[0] is None:
            grid_run.status = UKRiskGridRun.Status.FAILED
            grid_run.error_message = "Probe returned no ensemble members"
            grid_run.save()
            raise CommandError(
                "Ensemble probe returned no members. Check connectivity and "
                f"the endpoint: {ens.ensemble_url()}"
            )

        num_hours = len(ref_times)
        self.stdout.write(
            f"  Timestamp axis: {num_hours} hours, "
            f"{len(probe_points[0])} members per point"
        )

        # ==============================================================
        # PHASE 2: ALLOCATE NUMPY ACCUMULATORS
        # ==============================================================
        # Sums of member values, for the displayed weather layers, plus a
        # per-variable count so a variable missing from some members is
        # divided by what actually contributed rather than by the total.
        acc_wind = np.zeros((total_points, num_hours), dtype=np.float32)
        acc_gust = np.zeros((total_points, num_hours), dtype=np.float32)
        acc_prcp = np.zeros((total_points, num_hours), dtype=np.float32)
        acc_temp = np.zeros((total_points, num_hours), dtype=np.float32)

        wt_wind = np.zeros((total_points, num_hours), dtype=np.float32)
        wt_gust = np.zeros((total_points, num_hours), dtype=np.float32)
        wt_prcp = np.zeros((total_points, num_hours), dtype=np.float32)
        wt_temp = np.zeros((total_points, num_hours), dtype=np.float32)

        # Members breaching any cancel limit, and members present at all.
        # Their ratio is the chance of cancellation.
        breach_counts = np.zeros((total_points, num_hours), dtype=np.int16)
        member_counts = np.zeros((total_points,), dtype=np.int16)

        mem_mb = total_points * num_hours * 4 * 9 / 1024 / 1024
        self.stdout.write(
            f"  Accumulators: {total_points}×{num_hours} = {mem_mb:.1f} MB"
        )

        # ==============================================================
        # PHASE 3: FETCH EACH MODEL, ACCUMULATE, DISCARD
        # ==============================================================
        api_calls_made = 0
        successful_models = [ens.GRID_ENSEMBLE]
        total_pts_ok = 0
        total_pts_fail = 0

        # Cancel limits, hoisted out of the loop.
        c_wind = thresholds.get("wind_mean_cancel")
        c_gust = thresholds.get("gust_cancel")
        c_prcp = thresholds.get("precip_cancel")
        c_cold = thresholds.get("temp_min_cancel")
        c_hot = thresholds.get("temp_max_cancel")

        VAR_SLOTS = (
            ("wind_speed_10m", acc_wind, wt_wind),
            ("wind_gusts_10m", acc_gust, wt_gust),
            ("precipitation", acc_prcp, wt_prcp),
            ("temperature_2m", acc_temp, wt_temp),
        )

        batches = [
            grid_points[i:i + batch_size]
            for i in range(0, total_points, batch_size)
        ]
        n_batches = len(batches)
        self.stdout.write(
            f"\n  Fetching {ens.GRID_ENSEMBLE} "
            f"({total_points} pts, {n_batches} batches)..."
        )

        queue = list(batches)
        deferred = []
        retry_swept = False
        batch_delay = BATCH_DELAY
        processed = 0

        while queue or (deferred and not retry_swept):
            # Sweep whatever the limiter took once the main pass is done and
            # the quota has had a chance to recover. Checked here rather than
            # at the end of the body so a batch that fails and `continue`s
            # cannot exit the loop with work still deferred.
            if not queue:
                retry_swept = True
                self.stdout.write(self.style.WARNING(
                    f"\n  {len(deferred)} batch(es) lost to rate limiting — "
                    f"retrying after {RETRY_PASS_COOLDOWN}s"
                ))
                time.sleep(RETRY_PASS_COOLDOWN)
                queue = deferred
                deferred = []

            batch_pts = queue.pop(0)
            processed += 1
            batch_label = (
                f"retry {processed - n_batches}" if processed > n_batches
                else f"{processed}/{n_batches}"
            )

            try:
                batch_times, per_point = ens.fetch_grid_members(
                    [p[0] for p in batch_pts],
                    [p[1] for p in batch_pts],
                    forecast_days=num_days,
                )
                api_calls_made += 1
            except Exception as e:
                if not _is_rate_limited(e):
                    self.stdout.write(self.style.ERROR(
                        f"    Batch {batch_label} failed: {scrub_key(e)}"
                    ))
                    total_pts_fail += len(batch_pts)
                    continue

                # Slow the rest of the run down. Without this the limiter
                # keeps biting and every later batch is lost the same way.
                batch_delay = min(batch_delay * BACKOFF_FACTOR, BATCH_DELAY_MAX)

                # Worth waiting out rather than dropping: the alternative is
                # a hole in the map where the limiter happened to bite.
                self.stdout.write(self.style.WARNING(
                    f"    Rate limited on batch {batch_label} — "
                    f"waiting {RATE_LIMIT_WAIT}s "
                    f"(pacing now {batch_delay:.0f}s)"
                ))
                time.sleep(RATE_LIMIT_WAIT)
                try:
                    batch_times, per_point = ens.fetch_grid_members(
                        [p[0] for p in batch_pts],
                        [p[1] for p in batch_pts],
                        forecast_days=num_days,
                    )
                    api_calls_made += 1
                except Exception as retry_e:
                    if retry_swept:
                        self.stdout.write(self.style.ERROR(
                            f"    Retry failed: {scrub_key(retry_e)}"
                        ))
                        total_pts_fail += len(batch_pts)
                    else:
                        self.stdout.write(self.style.WARNING(
                            f"    Batch {batch_label} still limited — "
                            f"deferred to the retry sweep"
                        ))
                        deferred.append(batch_pts)
                    continue

            # Every point must sit on the axis the accumulators were sized
            # for; a batch on a different axis would be written to the wrong
            # hours, so drop it rather than misalign it.
            if batch_times != ref_times:
                self.stdout.write(self.style.WARNING(
                    f"    Batch {batch_label} returned a different "
                    f"time axis — skipped"
                ))
                total_pts_fail += len(batch_pts)
                continue

            for j, members in enumerate(per_point):
                if not members:
                    total_pts_fail += 1
                    continue

                idx = point_index.get(batch_pts[j])
                if idx is None:
                    total_pts_fail += 1
                    continue

                stacked = {}
                for var, acc, wt in VAR_SLOTS:
                    arr = np.array(
                        [[np.nan if v is None else v for v in m[var]] for m in members],
                        dtype=np.float32,
                    )
                    finite = np.isfinite(arr)
                    acc[idx] += np.where(finite, arr, 0.0).sum(axis=0)
                    wt[idx] += finite.sum(axis=0)
                    stacked[var] = arr

                # A member cancels this hour if ANY variable breaches its
                # cancel limit. Evaluated per member so correlation between
                # variables is carried by the scenario, not assumed.
                # NaN compares False throughout, which correctly reads as
                # "no evidence of a breach" rather than inventing one.
                breach = np.zeros(stacked["wind_gusts_10m"].shape, dtype=bool)
                if c_wind is not None:
                    breach |= stacked["wind_speed_10m"] > c_wind
                if c_gust is not None:
                    breach |= stacked["wind_gusts_10m"] > c_gust
                if c_prcp is not None:
                    breach |= stacked["precipitation"] > c_prcp
                if c_cold is not None:
                    breach |= stacked["temperature_2m"] <= c_cold
                if c_hot is not None:
                    breach |= stacked["temperature_2m"] >= c_hot

                breach_counts[idx] = breach.sum(axis=0)
                member_counts[idx] = len(members)
                total_pts_ok += 1

            del per_point
            gc.collect()

            if processed % 5 == 0 or not queue:
                self.stdout.write(
                    f"    Batch {batch_label} complete "
                    f"({total_pts_ok} OK, {total_pts_fail} failed)"
                )

            time.sleep(batch_delay)

        if not total_pts_ok:
            grid_run.status = UKRiskGridRun.Status.FAILED
            grid_run.error_message = (
                f"No grid point returned ensemble members. "
                f"API calls made: {api_calls_made}."
            )
            grid_run.save()
            raise CommandError(
                f"Ensemble fetch produced nothing after {api_calls_made} calls. "
                f"Host: {ens.ENSEMBLE_HOST}"
            )

        self.stdout.write(self.style.SUCCESS(
            f"  {ens.GRID_ENSEMBLE}: {total_pts_ok} pts OK"
            + (f", {total_pts_fail} failed" if total_pts_fail else "")
        ))

        # ==============================================================
        # PHASE 4: NORMALISE AND COMPUTE RISK SCORES
        # ==============================================================
        self.stdout.write(
            f"\n  Averaging {total_pts_ok} points and computing "
            f"chance of cancellation..."
        )

        # Mean of the members that actually reported, per variable. Counting
        # per variable matters: a member can carry a valid wind series and a
        # null temperature for the same hour, and dividing by the total member
        # count would then bias that variable low.
        def _normalise(acc, wt):
            with np.errstate(invalid="ignore", divide="ignore"):
                return np.where(wt > 0, acc / np.where(wt > 0, wt, 1.0), np.nan)

        acc_wind = _normalise(acc_wind, wt_wind)
        acc_gust = _normalise(acc_gust, wt_gust)
        acc_prcp = _normalise(acc_prcp, wt_prcp)
        acc_temp = _normalise(acc_temp, wt_temp)

        # A cell is usable only when every input variable is present.
        mask = (wt_wind > 0) & (wt_gust > 0) & (wt_prcp > 0) & (wt_temp > 0)

        # Chance of cancellation: the share of this point's members breaching
        # any cancel limit at that hour, as a percentage. Points that returned
        # no members stay NaN and are skipped, rather than reading as 0%,
        # which would render as a reassuring green.
        with np.errstate(invalid="ignore", divide="ignore"):
            members_col = member_counts[:, None].astype(np.float32)
            p_cancel_grid = np.where(
                members_col > 0,
                breach_counts / np.where(members_col > 0, members_col, 1.0) * 100.0,
                np.nan,
            )

        del wt_wind, wt_gust, wt_prcp, wt_temp, breach_counts
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

                risk = calculate_hourly_risk(w, g, p, t, thresholds)

                # Defensive: never persist a NaN risk. It would serialise as
                # an invalid JSON `NaN` token and break the map client.
                if not np.isfinite(risk):
                    continue

                pc = float(p_cancel_grid[pt_idx, t_idx])

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
                    # Null rather than 0 when unknown — see the note above.
                    p_cancel=round(pc, 2) if np.isfinite(pc) else None,
                    ensemble_members=int(member_counts[pt_idx]) or None,
                ))

                # Flush to DB periodically to limit in-memory records
                if len(all_point_records) >= DB_BATCH_SIZE:
                    UKRiskGridPoint.objects.bulk_create(
                        all_point_records, batch_size=1000
                    )
                    records_flushed += len(all_point_records)
                    self.stdout.write(
                        f"    Flushed {records_flushed} records to DB "
                        f"({pt_idx + 1}/{total_points} points processed)"
                    )
                    all_point_records = []

        # Flush remaining
        if all_point_records:
            UKRiskGridPoint.objects.bulk_create(
                all_point_records, batch_size=1000
            )
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

        # ==============================================================
        # PHASE 4b: PRE-RENDER CONTOUR OVERLAYS
        # ==============================================================
        if contour_vars:
            self._render_contours(grid_run, contour_vars)

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

        # Enforce a retention window. Without this, every run's grid points
        # (and now contour images) accumulate forever — at 0.5° that is tens
        # of thousands of rows per run, four runs a day.
        cutoff = today - timedelta(days=retention_days)
        stale = UKRiskGridRun.objects.filter(forecast_date__lt=cutoff)
        stale_count = stale.count()
        if stale_count:
            stale.delete()
            self.stdout.write(
                f"  Retention: removed {stale_count} run(s) older than "
                f"{retention_days} day(s)"
            )

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
            f"from {', '.join(successful_models)}"
        ))

        return grid_run
