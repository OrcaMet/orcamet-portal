"""
OrcaMet Forecast Engine — Core

Extracted from Point_Job_Certainty.py and adapted for Django.
Fetches multi-model ensemble forecasts from Open-Meteo, blends them
with geographic-aware weighting, and computes hourly risk scores.
"""

import logging
import math
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import pandas as pd
import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# ============================================================
# HTTP SESSION WITH RETRY/BACKOFF
# ============================================================
# Open-Meteo occasionally returns 429/5xx or drops a connection under
# load. Retry transient failures instead of failing the whole run.

def _build_session() -> requests.Session:
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_session = _build_session()
# Base host for the Open-Meteo API (used for logging/diagnostics by the
# grid management command).
OPENMETEO_HOST = "https://api.open-meteo.com"

# A paid key is served from a different host. Open-Meteo's docs: "The server
# URL requires the prefix customer-."
#
# Sending the key to the free host does not fail — it is simply ignored, and
# the request is served as anonymous traffic on free-tier limits. That is how
# this hid: risk_grid printed "API key: ****abcd" on every run while all 38
# of its ensemble calls were being rate limited as though no key existed,
# losing every grid point north of 55.9N.
CUSTOMER_PREFIX = "customer-"


def customer_url(url, api_key=None):
    """
    Rewrite an Open-Meteo URL onto the customer host when a key is set.

    Without a key the free host is returned unchanged, so an unkeyed
    deployment keeps working exactly as before.
    """
    if api_key is None:
        api_key = getattr(settings, "OPENMETEO_API_KEY", "")
    if not api_key:
        return url

    parts = urlsplit(url)
    if parts.netloc.startswith(CUSTOMER_PREFIX):
        return url
    return urlunsplit(parts._replace(netloc=CUSTOMER_PREFIX + parts.netloc))


def openmeteo_host():
    """The base host actually in use, for diagnostics."""
    return customer_url(OPENMETEO_HOST)


# requests puts the full request URL into its exception text, and the key
# now rides in the query string. Untouched, a single 429 would print the
# credential into the Render logs and, for the grid, persist it in
# UKRiskGridRun.error_message.
_APIKEY_RE = re.compile(r"(apikey=)[^&\s]+", re.IGNORECASE)


def scrub_key(text):
    """Redact the API key from anything about to be logged or stored."""
    return _APIKEY_RE.sub(lambda m: m.group(1) + "***", str(text))


# ============================================================
# MULTI-MODEL CONFIGURATION
# ============================================================

MODELS_CONFIG = {
    "ukv": {
        "name": "Met Office UKV",
        "url": "https://api.open-meteo.com/v1/forecast",
        "params": {"models": "ukmo_uk_deterministic_2km"},
        "resolution_km": 2.0,
    },
    "ecmwf": {
        "name": "ECMWF HRES",
        "url": "https://api.open-meteo.com/v1/ecmwf",
        "params": {},
        "resolution_km": 9.0,
    },
    "icon_eu": {
        "name": "DWD ICON-EU",
        "url": "https://api.open-meteo.com/v1/dwd-icon",
        "params": {},
        "resolution_km": 7.0,
    },
    "arpege_europe": {
        "name": "Météo-France ARPEGE",
        "url": "https://api.open-meteo.com/v1/meteofrance",
        "params": {"models": "arpege_europe"},
        "resolution_km": 10.0,
    },
}

# ============================================================
# MODEL GEOGRAPHIC DOMAINS
# ============================================================
# Approximate coverage boxes for each model. UKV is a UK/Ireland-only
# limited-area model; the others are pan-European/global and comfortably
# cover the UK risk grid. Used by risk_grid.py to skip models that don't
# cover a given grid point.

DOMAIN_BOUNDS = {
    "ukv": {"lat_min": 49.0, "lat_max": 61.0, "lon_min": -11.0, "lon_max": 2.0},
    "ecmwf": None,  # global model — always in domain
    "icon_eu": {"lat_min": 29.5, "lat_max": 70.5, "lon_min": -23.5, "lon_max": 45.0},
    "arpege_europe": {"lat_min": 20.0, "lat_max": 72.0, "lon_min": -32.0, "lon_max": 42.0},
}

def is_in_domain(model_name: str, lat: float, lon: float) -> bool:
    """Return True if (lat, lon) falls inside the model's coverage domain."""
    bounds = DOMAIN_BOUNDS.get(model_name)
    if bounds is None:
        return True
    return bounds["lat_min"] <= lat <= bounds["lat_max"] and bounds["lon_min"] <= lon <= bounds["lon_max"]


# ============================================================
# GEOGRAPHIC-AWARE MODEL WEIGHTING
# ============================================================

def get_model_weights(lat: float, lon: float, exposure: str = "urban") -> dict:
    """Determine model weights based on geographic location and site exposure."""

    scotland = lat > 56.0
    northern_england = 53.5 < lat <= 56.0
    coastal = exposure == "coastal"
    highland = exposure == "highland"

    if highland or scotland:
        return {"ukv": 0.60, "ecmwf": 0.25, "icon_eu": 0.10, "arpege_europe": 0.05}
    elif coastal:
        return {"ukv": 0.45, "ecmwf": 0.25, "arpege_europe": 0.20, "icon_eu": 0.10}
    elif northern_england:
        return {"ukv": 0.40, "ecmwf": 0.30, "icon_eu": 0.20, "arpege_europe": 0.10}
    else:
        return {"ukv": 0.35, "ecmwf": 0.35, "icon_eu": 0.20, "arpege_europe": 0.10}


# ============================================================
# RISK MODEL
# ============================================================

def sigmoid(x: float) -> float:
    """Sigmoid activation for risk scoring."""
    return 1.0 / (1.0 + math.exp(-x))


def ramp(value: float, soft: float, hard: float, high_bad: bool = True) -> float:
    """Linear ramp between soft and hard thresholds."""
    if np.isnan(value):
        return np.nan
    if high_bad:
        if value <= soft:
            return 0.0
        if value >= hard:
            return 1.0
        return (value - soft) / (hard - soft)
    else:
        if value >= soft:
            return 0.0
        if value <= hard:
            return 1.0
        return (soft - value) / (soft - hard)


def temperature_ramp(temp: float, thresholds: dict) -> float:
    """
    Two-sided temperature severity: cold at one end, heat at the other.

    Rope access is limited by both — numb hands and ice below, heat stress
    under PPE above. Returns whichever end is worse, so the pair shares the
    single temperature weight in calculate_hourly_risk rather than letting
    temperature quietly count twice.

    The heat thresholds are optional. A thresholds dict without them, or with
    them set to None, scores cold only — which is what every forecast did
    before heat existed, and keeps historic ForecastRun.thresholds_snapshot
    values replayable.
    """
    cold = ramp(temp, thresholds["temp_min_caution"], thresholds["temp_min_cancel"],
                high_bad=False)

    hot_caution = thresholds.get("temp_max_caution")
    hot_cancel = thresholds.get("temp_max_cancel")
    if hot_caution is None or hot_cancel is None:
        return cold

    heat = ramp(temp, hot_caution, hot_cancel, high_bad=True)

    # max() would pick the number over a NaN, hiding missing data as a real
    # low-risk reading.
    if np.isnan(cold) or np.isnan(heat):
        return np.nan
    return max(cold, heat)


def calculate_hourly_risk(wind: float, gust: float, precip: float, temp: float,
                          thresholds: dict = None) -> float:
    """
    Calculate instantaneous hourly risk score (0-100%).

    Uses site-specific thresholds if provided, otherwise falls back to defaults.
    """
    if thresholds is None:
        thresholds = {
            "wind_mean_caution": 10.0, "wind_mean_cancel": 14.0,
            "gust_caution": 15.0, "gust_cancel": 20.0,
            "precip_caution": 0.7, "precip_cancel": 2.0,
            "temp_min_caution": 1.0, "temp_min_cancel": -2.0,
            "temp_max_caution": 27.0, "temp_max_cancel": 32.0,
        }

    r = (
        0.30 * ramp(wind, thresholds["wind_mean_caution"], thresholds["wind_mean_cancel"], high_bad=True) +
        0.40 * ramp(gust, thresholds["gust_caution"], thresholds["gust_cancel"], high_bad=True) +
        0.20 * ramp(precip, thresholds["precip_caution"], thresholds["precip_cancel"], high_bad=True) +
        0.10 * temperature_ramp(temp, thresholds)
    )

    if np.isnan(r):
        return np.nan

    prob = sigmoid(6.0 * (r - 0.45))
    return float(np.clip(prob * 100, 0.0, 100.0))


# Order matters only for reporting: the first breach found at the worst
# level is the one named as limiting.
GATE_VARIABLES = (
    ("gust", "gust_caution", "gust_cancel"),
    ("wind", "wind_mean_caution", "wind_mean_cancel"),
    ("precip", "precip_caution", "precip_cancel"),
)


def evaluate_thresholds(wind, gust, precip, temp, thresholds):
    """
    Hard-gate verdict for one hour: does anything breach a limit?

    Returns (verdict, limiting_variable).

    This replaces the weighted severity score for deciding GO / CAUTION /
    CANCEL. The weighted form could not express a breach at all: gusts carry
    0.40 on a scale centred at 0.45, so gusts alone topped out at 42.6% —
    CAUTION — no matter how extreme, and rain or temperature alone never left
    GO at any magnitude. A limit that cannot stop work is not a limit.

    The severity score remains available for ranking hours within a band; it
    is no longer what decides the band.
    """
    values = {"gust": gust, "wind": wind, "precip": precip}

    worst = "GO"
    limiting = None

    for name, caution_key, cancel_key in GATE_VARIABLES:
        value = values[name]
        if value is None or not np.isfinite(value):
            continue

        cancel = thresholds.get(cancel_key)
        if cancel is not None and value >= cancel:
            # Cancel outranks anything already found; first one wins the name.
            if worst != "CANCEL":
                worst, limiting = "CANCEL", name
            continue

        caution = thresholds.get(caution_key)
        if caution is not None and value >= caution and worst == "GO":
            worst, limiting = "CAUTION", name

    # Temperature is two-sided — too cold or too hot both stop work.
    if temp is not None and np.isfinite(temp):
        cold_cancel = thresholds.get("temp_min_cancel")
        hot_cancel = thresholds.get("temp_max_cancel")
        cold_caution = thresholds.get("temp_min_caution")
        hot_caution = thresholds.get("temp_max_caution")

        if (cold_cancel is not None and temp <= cold_cancel) or \
                (hot_cancel is not None and temp >= hot_cancel):
            if worst != "CANCEL":
                worst, limiting = "CANCEL", "temperature"
        elif worst == "GO" and (
            (cold_caution is not None and temp <= cold_caution)
            or (hot_caution is not None and temp >= hot_caution)
        ):
            worst, limiting = "CAUTION", "temperature"

    return worst, limiting


def get_recommendation(risk: float) -> str:
    """Convert risk score to recommendation string."""
    if np.isnan(risk):
        return "UNKNOWN"
    if risk < 20:
        return "GO"
    elif risk < 50:
        return "CAUTION"
    else:
        return "CANCEL"


# ============================================================
# API FETCHING
# ============================================================

def fetch_single_model(model_name: str, lat: float, lon: float,
                       start_date: str, end_date: str) -> dict:
    """Fetch hourly data from a single weather model via Open-Meteo."""

    config = MODELS_CONFIG[model_name]
    api_key = getattr(settings, "OPENMETEO_API_KEY", "")

    params = {
        "latitude": lat,
        "longitude": lon,
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

    resp = _session.get(
        customer_url(config["url"], api_key), params=params, timeout=30
    )
    resp.raise_for_status()
    j = resp.json()

    h = j.get("hourly", {})
    if not h or "time" not in h:
        raise ValueError(f"No hourly data returned for {model_name}")

    return {
        "model": model_name,
        "time": h["time"],
        "wind_speed": h.get("wind_speed_10m", []),
        "wind_gusts": h.get("wind_gusts_10m", []),
        "precipitation": h.get("precipitation", []),
        "temperature": h.get("temperature_2m", []),
    }


def fetch_ensemble(lat: float, lon: float, exposure: str,
                   start_date: str, end_date: str) -> pd.DataFrame:
    """
    Fetch multi-model ensemble for a location and blend into a single DataFrame.

    Returns DataFrame with columns: time, wind_speed, wind_gusts, precipitation,
    temperature, and _spread columns for each, plus n_models.
    """
    weights = get_model_weights(lat, lon, exposure)

    ensemble_data = {}
    successful_models = []

    for model_name, weight in weights.items():
        if model_name not in MODELS_CONFIG:
            continue
        try:
            data = fetch_single_model(model_name, lat, lon, start_date, end_date)
            ensemble_data[model_name] = {"weight": weight, "data": data}
            successful_models.append(model_name)
            logger.debug(f"  ✓ {MODELS_CONFIG[model_name]['name']}")
        except Exception as e:
            logger.warning(
                f"  ✗ {MODELS_CONFIG[model_name]['name']}: {scrub_key(e)}"
            )

        # Be polite to the API
        time.sleep(0.15)

    if not ensemble_data:
        raise ValueError(f"All models failed for ({lat:.4f}, {lon:.4f})")

    # Re-normalise weights to account for failed models
    total_weight = sum(d["weight"] for d in ensemble_data.values())
    for model_name in ensemble_data:
        ensemble_data[model_name]["weight"] /= total_weight

    return _create_weighted_ensemble(ensemble_data, successful_models)


ENSEMBLE_VARS = ("wind_speed", "wind_gusts", "precipitation", "temperature")


def _to_float_array(values, n_times: int) -> np.ndarray:
    """
    Convert a raw API value list to a float array of length n_times.

    Open-Meteo returns JSON null for missing hours; those become NaN so they
    can be excluded from the blend rather than treated as real readings.
    Returns None if the series cannot be used.
    """
    if values is None:
        return None
    arr = np.array(
        [np.nan if v is None else v for v in values],
        dtype=float,
    )
    if arr.shape[0] != n_times:
        return None
    return arr


def _create_weighted_ensemble(ensemble_data: dict, model_names: list) -> pd.DataFrame:
    """
    Blend multiple model outputs into a weighted ensemble DataFrame.

    Each variable is averaged using only the weight of the models that
    actually contributed a finite value at that hour. Previously the weights
    were normalised across every fetched model while models that returned a
    mismatched or missing series were silently dropped from the sum, so the
    blended values were divided by more weight than was applied — biasing
    wind, gusts and precipitation low, and under-stating risk.
    """

    ref_data = list(ensemble_data.values())[0]["data"]
    times = pd.to_datetime(ref_data["time"], utc=True)
    n_times = len(times)

    weighted_sums = {var: np.zeros(n_times) for var in ENSEMBLE_VARS}
    weight_sums = {var: np.zeros(n_times) for var in ENSEMBLE_VARS}
    raw_values = {var: [] for var in ENSEMBLE_VARS}

    for model_name, model_info in ensemble_data.items():
        weight = model_info["weight"]
        data = model_info["data"]

        for var in ENSEMBLE_VARS:
            values = _to_float_array(data.get(var), n_times)
            if values is None:
                logger.warning(
                    f"  {model_name}: unusable '{var}' series "
                    f"(expected {n_times} values) — excluded from blend"
                )
                continue

            finite = np.isfinite(values)
            weighted_sums[var][finite] += weight * values[finite]
            weight_sums[var][finite] += weight
            raw_values[var].append(values)

    # Divide by the weight actually applied at each hour. Hours where no
    # model contributed stay NaN rather than silently reading as zero.
    ensemble_vars = {}
    for var in ENSEMBLE_VARS:
        with np.errstate(invalid="ignore", divide="ignore"):
            ensemble_vars[var] = np.where(
                weight_sums[var] > 0,
                weighted_sums[var] / np.where(weight_sums[var] > 0, weight_sums[var], 1.0),
                np.nan,
            )

    spread = {}
    for var, vals_list in raw_values.items():
        if len(vals_list) > 1:
            stacked = np.vstack(vals_list)
            with np.errstate(invalid="ignore"):
                # All-NaN columns would otherwise emit a RuntimeWarning.
                counts = np.sum(np.isfinite(stacked), axis=0)
                std = np.full(n_times, np.nan)
                usable = counts > 0
                if usable.any():
                    std[usable] = np.nanstd(stacked[:, usable], axis=0)
            spread[f"{var}_spread"] = std
        else:
            spread[f"{var}_spread"] = np.zeros(n_times)

    df = pd.DataFrame({
        "time": times,
        **ensemble_vars,
        **spread,
        "n_models": len(model_names),
    })
    df.attrs["models_used"] = model_names
    return df
