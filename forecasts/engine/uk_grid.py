"""
OrcaMet Forecast Engine — UK Grid

Builds a coarse UK-wide grid and fetches a batched multi-model ensemble
for every point in as few Open-Meteo requests as possible (one request
per model per chunk of points, using Open-Meteo's multi-location
support), then blends per-point using the same geographic weighting as
single-site forecasts. Feeds the UK risk contour map (Phase 5).
"""

import logging
import time
from datetime import datetime

import numpy as np
from django.conf import settings

from .core import MODELS_CONFIG, get_model_weights, calculate_hourly_risk, _session

logger = logging.getLogger(__name__)

# Rough UK mainland + islands bounding box
UK_BOUNDS = {
    "lat_min": 49.9, "lat_max": 60.9,
    "lon_min": -8.6, "lon_max": 1.8,
}

GRID_SPACING_DEG = getattr(settings, "UK_GRID_SPACING_DEG", 1.0)
GRID_BATCH_SIZE = getattr(settings, "UK_GRID_BATCH_SIZE", 50)


def build_grid(spacing: float = GRID_SPACING_DEG) -> list:
    """Return a list of (lat, lon) points covering the UK bounding box."""
    lats = np.arange(UK_BOUNDS["lat_min"], UK_BOUNDS["lat_max"] + spacing, spacing)
    lons = np.arange(UK_BOUNDS["lon_min"], UK_BOUNDS["lon_max"] + spacing, spacing)
    return [(round(float(lat), 3), round(float(lon), 3)) for lat in lats for lon in lons]


def _chunk(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_grid_model(model_name: str, points: list, start_date: str, end_date: str) -> dict:
    """
    Fetch one model's hourly data for a batch of points in a single
    request, using Open-Meteo's comma-separated multi-location support.
    """
    config = MODELS_CONFIG[model_name]
    api_key = getattr(settings, "OPENMETEO_API_KEY", "")

    params = {
        "latitude": ",".join(str(p[0]) for p in points),
        "longitude": ",".join(str(p[1]) for p in points),
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

    resp = _session.get(config["url"], params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    # A single-location request returns one dict; multi-location returns a list.
    if isinstance(data, dict):
        data = [data]

    results = {}
    for point, entry in zip(points, data):
        h = entry.get("hourly", {})
        if not h or "time" not in h:
            continue
        results[point] = {
            "time": h["time"],
            "wind_speed": h.get("wind_speed_10m", []),
            "wind_gusts": h.get("wind_gusts_10m", []),
            "precipitation": h.get("precipitation", []),
            "temperature": h.get("temperature_2m", []),
        }
    return results


def fetch_grid_ensemble(points: list, start_date: str, end_date: str) -> dict:
    """
    Fetch all configured models for every grid point (batched per model),
    then blend per-point using the same geographic weighting as
    single-site forecasts.

    Returns {(lat, lon): {"time": [...], "wind_speed": [...], ...}}.
    """
    per_model = {}
    for model_name in MODELS_CONFIG:
        model_points_data = {}
        for chunk in _chunk(points, GRID_BATCH_SIZE):
            try:
                model_points_data.update(
                    fetch_grid_model(model_name, chunk, start_date, end_date)
                )
            except Exception as e:
                logger.warning(f"Grid fetch failed for {model_name} chunk: {e}")
            time.sleep(0.2)
        per_model[model_name] = model_points_data
        logger.info(
            f"  {MODELS_CONFIG[model_name]['name']}: "
            f"{len(model_points_data)}/{len(points)} points"
        )

    blended = {}
    for point in points:
        weights = get_model_weights(point[0], point[1])
        available = {
            m: per_model[m][point]
            for m in MODELS_CONFIG
            if point in per_model.get(m, {})
        }
        if not available:
            continue

        total_weight = sum(weights[m] for m in available)
        ref_time = next(iter(available.values()))["time"]
        n = len(ref_time)

        blend = {
            "wind_speed": np.zeros(n), "wind_gusts": np.zeros(n),
            "precipitation": np.zeros(n), "temperature": np.zeros(n),
        }
        for model_name, data in available.items():
            w = weights[model_name] / total_weight
            for var in blend:
                values = np.array(data.get(var) or [np.nan] * n, dtype=float)
                if len(values) == n:
                    blend[var] += w * values

        blend["time"] = ref_time
        blended[point] = blend

    return blended


def compute_grid_risk(blended: dict, work_start: int, work_end: int) -> dict:
    """Reduce each point's blended hourly series to a single peak risk score."""
    risk_by_point = {}
    for point, data in blended.items():
        peak = None
        for i, t in enumerate(data["time"]):
            hour = datetime.fromisoformat(t).hour
            if hour < work_start or hour > work_end:
                continue
            risk = calculate_hourly_risk(
                data["wind_speed"][i], data["wind_gusts"][i],
                data["precipitation"][i], data["temperature"][i],
            )
            if not np.isnan(risk):
                peak = risk if peak is None else max(peak, risk)
        if peak is not None:
            risk_by_point[point] = peak
    return risk_by_point
