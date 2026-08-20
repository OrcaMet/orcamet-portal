"""
OrcaMet Forecast Engine — Ensemble probability of cancellation.

The deterministic engine in core.py blends four models into one value and
scores its severity. That answers "how bad is the central forecast", not
"how likely is this job to be called off" — and because the blend happens
first, the disagreement between models, which is exactly what makes a breach
likely or unlikely, is gone before anything is scored.

This module keeps the members. Each member of an ensemble is one coherent
simulated atmosphere: wind_gusts_10m_member01 and wind_speed_10m_member01 are
the same scenario, so we can ask each member whether IT would have cancelled,
rather than combining per-variable probabilities as if they were independent.

P(cancellation) = share of members in which any variable breaches any cancel
limit at any hour of the work window.
"""

import logging
from datetime import datetime, timezone

import numpy as np
from django.conf import settings

from .core import _session

logger = logging.getLogger(__name__)

ENSEMBLE_HOST = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Member counts verified against the live API: ECMWF 51, ICON 40, GFS 31.
# Pooled, they give a distribution wide enough to read probabilities off.
#
# UKMO's UK 2km ensemble is deliberately absent: it exposes only 3 members,
# which cannot carry a probability and would be swamped anyway. The
# deterministic UKV fetch in core.py remains the high-resolution input, and
# still drives the values and therefore the verdict.
ENSEMBLE_MODELS = ("ecmwf_ifs025_ensemble", "icon_seamless", "gfs_seamless")

# Open-Meteo variable names, mapped to the threshold keys they are tested
# against. Temperature is two-sided and handled separately.
VARIABLES = {
    "wind_speed_10m": ("wind_mean_caution", "wind_mean_cancel"),
    "wind_gusts_10m": ("gust_caution", "gust_cancel"),
    "precipitation": ("precip_caution", "precip_cancel"),
    "temperature_2m": None,
}

HOURLY_VARS = ",".join(VARIABLES)

REQUEST_TIMEOUT = 90


class EnsembleUnavailable(RuntimeError):
    """No ensemble member data could be retrieved."""


def fetch_members(lat, lon, forecast_days=3, models=ENSEMBLE_MODELS):
    """
    Fetch ensemble members for one point.

    Returns (times, members) where `times` is a list of ISO strings and
    `members` is a list of dicts, one per member, each mapping a variable
    name to a list of values aligned with `times`.

    Members from different ensembles are pooled. Within one ensemble a
    member is a coherent scenario; across ensembles they are simply
    independent samples of the same forecast problem, which is what we want.
    """
    times = None
    members = []

    for model in models:
        try:
            resp = _session.get(
                ENSEMBLE_HOST,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": HOURLY_VARS,
                    "models": model,
                    "wind_speed_unit": "ms",
                    "precipitation_unit": "mm",
                    "timezone": "UTC",
                    "forecast_days": forecast_days,
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            hourly = resp.json().get("hourly", {})
        except Exception as e:
            logger.warning("Ensemble %s failed at (%.3f, %.3f): %s", model, lat, lon, e)
            continue

        model_times = hourly.get("time")
        if not model_times:
            continue

        if times is None:
            times = model_times
        elif model_times != times:
            # Pooling depends on every member sharing one time axis. Rather
            # than silently blending scenarios from different hours, drop the
            # ensemble that disagrees.
            logger.warning(
                "Ensemble %s returned a different time axis — excluded", model
            )
            continue

        # Member suffixes are consistent across variables within a request,
        # so member01's wind and gusts describe the same atmosphere.
        suffixes = sorted(
            key[len("wind_gusts_10m"):]
            for key in hourly
            if key.startswith("wind_gusts_10m")
        )

        for suffix in suffixes:
            member = {}
            complete = True
            for var in VARIABLES:
                values = hourly.get(f"{var}{suffix}")
                if values is None or len(values) != len(times):
                    complete = False
                    break
                member[var] = values
            if complete:
                members.append(member)

        logger.debug("Ensemble %s contributed %d members", model, len(suffixes))

    if not members or times is None:
        raise EnsembleUnavailable(
            f"No ensemble members available at ({lat:.4f}, {lon:.4f})"
        )

    return times, members


# The UK map uses ECMWF alone rather than all three ensembles.
#
# Two reasons. Rate limits are the binding constraint on the grid — a
# batched ensemble call is heavy enough to trip Open-Meteo's minutely limit,
# so a third of the calls matters more than a wider sample. And 51 members
# is already plenty to read a probability surface off at 0.5° spacing.
#
# The same logic answers the UKV question: at ~55 km grid cells, a 2 km
# deterministic model's detail is discarded by the interpolation anyway, so
# nothing real is lost by leaving it out of the map.
GRID_ENSEMBLE = "ecmwf_ifs025_ensemble"


def fetch_grid_members(lats, lons, forecast_days=2, model=GRID_ENSEMBLE):
    """
    Fetch ensemble members for a batch of grid points in one call.

    Returns (times, per_point) where per_point is a list aligned with the
    requested coordinates; each entry is a list of member dicts, or None if
    that location came back unusable.
    """
    resp = _session.get(
        ENSEMBLE_HOST,
        params={
            "latitude": ",".join(f"{v:.4f}" for v in lats),
            "longitude": ",".join(f"{v:.4f}" for v in lons),
            "hourly": HOURLY_VARS,
            "models": model,
            "wind_speed_unit": "ms",
            "precipitation_unit": "mm",
            "timezone": "UTC",
            "forecast_days": forecast_days,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()

    # A single coordinate returns an object; several return a list.
    if isinstance(payload, dict):
        payload = [payload]

    times = None
    per_point = []

    for entry in payload:
        hourly = (entry or {}).get("hourly") or {}
        point_times = hourly.get("time")
        if not point_times:
            per_point.append(None)
            continue

        if times is None:
            times = point_times
        elif point_times != times:
            # Positional accumulation into a shared axis is only valid if the
            # axes match. Drop the outlier rather than blend across hours.
            logger.warning("Grid point returned a different time axis — skipped")
            per_point.append(None)
            continue

        suffixes = sorted(
            k[len("wind_gusts_10m"):]
            for k in hourly
            if k.startswith("wind_gusts_10m")
        )

        members = []
        for suffix in suffixes:
            member = {}
            ok = True
            for var in VARIABLES:
                values = hourly.get(f"{var}{suffix}")
                if values is None or len(values) != len(times):
                    ok = False
                    break
                member[var] = values
            if ok:
                members.append(member)

        per_point.append(members or None)

    return times, per_point


def count_breaches(members, thresholds, n_hours):
    """
    Per-hour count of members breaching any cancel limit at one grid point.

    Returns a list of ints, length n_hours. Hourly rather than daily: the map
    is a per-timestamp surface, so the question is "what share of scenarios
    stop work at this hour", not "on this day".
    """
    counts = [0] * n_hours

    for member in members:
        for i in range(n_hours):
            if _member_breaches_at(member, i, thresholds, "cancel"):
                counts[i] += 1

    return counts


def _breaches(value, low, high):
    """Is this value outside the allowed band? None means no limit set."""
    if value is None:
        return False
    if low is not None and value <= low:
        return True
    if high is not None and value >= high:
        return True
    return False


def _member_breaches_at(member, index, thresholds, level):
    """
    Which variables this member breaches at one hour.

    `level` is "cancel" or "caution". Returns a set of threshold keys, so the
    headline probability can always be broken down by what caused it.
    """
    hit = set()

    for var, keys in VARIABLES.items():
        if var == "temperature_2m":
            continue
        _caution_key, cancel_key = keys
        key = cancel_key if level == "cancel" else _caution_key
        limit = thresholds.get(key)
        if limit is None:
            continue
        value = member[var][index]
        if value is not None and value > limit:
            hit.add(key)

    # Temperature is two-sided: too cold or too hot both stop work.
    cold_key = "temp_min_cancel" if level == "cancel" else "temp_min_caution"
    hot_key = "temp_max_cancel" if level == "cancel" else "temp_max_caution"
    temp = member["temperature_2m"][index]
    if _breaches(temp, thresholds.get(cold_key), thresholds.get(hot_key)):
        hit.add("temperature")

    return hit


def _work_hour_indices(times, work_start, work_end):
    """
    Indices of `times` that fall inside the local work window.

    The API is asked for UTC, but the window is a local-time concept — under
    British Summer Time a UTC hour test is an hour out.
    """
    import zoneinfo

    tz = zoneinfo.ZoneInfo(settings.TIME_ZONE)
    by_day = {}

    for i, t in enumerate(times):
        moment = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        local = moment.astimezone(tz)
        if work_start <= local.hour <= work_end:
            by_day.setdefault(local.date(), []).append(i)

    return by_day


def cancellation_probability(times, members, thresholds,
                             work_start=None, work_end=None):
    """
    P(cancellation) per local day, from pooled ensemble members.

    Returns {date: {"p_cancel": float, "p_caution": float,
                    "by_variable": {threshold_key: float},
                    "members": int}}

    A member counts once per day however many hours or variables it breaches
    — the question is whether that scenario stops the job, not how often.
    """
    if work_start is None:
        work_start = getattr(settings, "FORECAST_WORK_START_HOUR", 7)
    if work_end is None:
        work_end = getattr(settings, "FORECAST_WORK_END_HOUR", 18)

    by_day = _work_hour_indices(times, work_start, work_end)
    total = len(members)
    result = {}

    for day, indices in by_day.items():
        cancelled = 0
        cautioned = 0
        by_variable = {}

        for member in members:
            cancel_causes = set()
            caution_hit = False

            for i in indices:
                cancel_causes |= _member_breaches_at(member, i, thresholds, "cancel")
                if not caution_hit and _member_breaches_at(
                    member, i, thresholds, "caution"
                ):
                    caution_hit = True

            if cancel_causes:
                cancelled += 1
                # Counted per member, not per hour, so the parts are
                # comparable with the headline figure.
                for key in cancel_causes:
                    by_variable[key] = by_variable.get(key, 0) + 1
            if caution_hit:
                cautioned += 1

        result[day] = {
            "p_cancel": cancelled / total,
            "p_caution": cautioned / total,
            "by_variable": {k: v / total for k, v in sorted(by_variable.items())},
            "members": total,
        }

    return result


def hourly_percentiles(times, members, variable):
    """
    p10 / p50 / p90 per hour for one variable, for the plume band on charts.

    More useful than the single standard deviation stored today, and honest
    about skew — precipitation is nowhere near symmetric.
    """
    out = []
    for i in range(len(times)):
        values = [
            m[variable][i] for m in members if m[variable][i] is not None
        ]
        if not values:
            out.append(None)
            continue
        arr = np.array(values, dtype=float)
        out.append({
            "p10": float(np.percentile(arr, 10)),
            "p50": float(np.percentile(arr, 50)),
            "p90": float(np.percentile(arr, 90)),
        })
    return out
