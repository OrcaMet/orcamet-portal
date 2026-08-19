"""
OrcaMet Portal — Map Interpolation Engine

Interpolates scattered UK grid points onto a smooth surface and renders
transparent contour PNGs for the interactive weather map's L.imageOverlay.
Used by risk_grid.py's --contour-vars pre-rendering step.

Public API:
    render_contour_to_bytes()    — Transparent PNG bytes for L.imageOverlay
    interpolate_risk_surface()   — Raw interpolation (returns grid arrays)
"""

import gc
import io
import logging

import numpy as np
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for server rendering
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from scipy.interpolate import CloughTocher2DInterpolator
from scipy.spatial import Delaunay

logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

INTERP_RESOLUTION = 200  # Default for overlays (lower = faster + less RAM)

# UK bounding box (matches risk_grid.py)
UK_LAT_MIN = 49.9
UK_LAT_MAX = 58.7
UK_LON_MIN = -7.6
UK_LON_MAX = 1.8

# Figure size for overlays (smaller = less memory)
OVERLAY_FIG_WIDTH = 6
OVERLAY_FIG_HEIGHT = 9
OVERLAY_DPI = 100

# Variable-specific colour map configuration
VARIABLE_CMAPS = {
    "risk":   {"cmap": "jet",       "vmin": 0,  "vmax": 100},
    "wind":   {"cmap": "YlOrRd",    "vmin": 0,  "vmax": 25},
    "gust":   {"cmap": "YlOrRd",    "vmin": 0,  "vmax": 35},
    "precip": {"cmap": "Blues",      "vmin": 0,  "vmax": 8},
    "temp":   {"cmap": "RdYlBu_r",  "vmin": -5, "vmax": 25},
}


# ============================================================
# INTERPOLATION
# ============================================================

def interpolate_risk_surface(lats, lons, values, resolution=INTERP_RESOLUTION):
    """
    Interpolate scattered data onto a regular grid using CloughTocher2D.
    Returns (grid_lons, grid_lats, grid_values) as 2D ndarrays.
    """
    valid = ~(np.isnan(lats) | np.isnan(lons) | np.isnan(values))
    lats, lons, values = lats[valid], lons[valid], values[valid]

    if len(lats) < 4:
        raise ValueError(f"Need >= 4 data points, got {len(lats)}")

    points = np.column_stack([lons, lats])
    tri = Delaunay(points)
    interpolator = CloughTocher2DInterpolator(tri, values, tol=1e-6)

    lat_range = UK_LAT_MAX - UK_LAT_MIN
    lon_range = UK_LON_MAX - UK_LON_MIN
    if lat_range >= lon_range:
        n_lat = resolution
        n_lon = int(resolution * lon_range / lat_range)
    else:
        n_lon = resolution
        n_lat = int(resolution * lat_range / lon_range)

    grid_lon_1d = np.linspace(UK_LON_MIN, UK_LON_MAX, n_lon)
    grid_lat_1d = np.linspace(UK_LAT_MIN, UK_LAT_MAX, n_lat)
    grid_lons, grid_lats = np.meshgrid(grid_lon_1d, grid_lat_1d)

    grid_pts = np.column_stack([grid_lons.ravel(), grid_lats.ravel()])
    grid_values = interpolator(grid_pts).reshape(grid_lons.shape)

    return grid_lons, grid_lats, grid_values


# ============================================================
# CONTOUR RENDERING (transparent PNG for L.imageOverlay)
# ============================================================

def render_contour_to_bytes(
    lats: np.ndarray,
    lons: np.ndarray,
    values: np.ndarray,
    variable: str = "risk",
    resolution: int = INTERP_RESOLUTION,
    dpi: int = OVERLAY_DPI,
) -> bytes:
    """
    Render a transparent contour PNG for L.imageOverlay.
    No axes, no chrome — just the contour fill.

    Handles edge cases:
    - All-constant data (e.g. precip = 0 everywhere)
    - NaN-heavy interpolation results
    """
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    values = np.asarray(values, dtype=float)

    cm = VARIABLE_CMAPS.get(variable, VARIABLE_CMAPS["risk"])

    # Interpolate
    grid_lons, grid_lats, grid_values = interpolate_risk_surface(
        lats, lons, values, resolution=resolution
    )

    # Clamp to variable range
    grid_values = np.clip(grid_values, cm["vmin"], cm["vmax"])

    # No land masking for overlays — the dark base map handles sea.
    # This avoids jagged coastline edges from low-res Natural Earth data.
    grid_values_masked = grid_values

    # Handle all-constant data: contourf needs at least some variation
    # in the levels that spans the data range. If data is constant,
    # the plot is just one solid colour — that's fine, but we need
    # to make sure the levels array doesn't confuse matplotlib.
    data_min = np.nanmin(grid_values_masked)
    data_max = np.nanmax(grid_values_masked)

    if np.isnan(data_min) or np.isnan(data_max):
        # All NaN — return a transparent 1×1 PNG
        fig, ax = plt.subplots(figsize=(1, 1))
        fig.patch.set_alpha(0)
        ax.axis("off")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", transparent=True)
        plt.close(fig)
        gc.collect()
        buf.seek(0)
        return buf.getvalue()

    # Build levels — always use the fixed variable range
    levels = np.linspace(cm["vmin"], cm["vmax"], 51)

    # Render
    fig, ax = plt.subplots(
        figsize=(OVERLAY_FIG_WIDTH, OVERLAY_FIG_HEIGHT), dpi=dpi
    )
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")

    try:
        ax.contourf(
            grid_lons, grid_lats, grid_values_masked,
            levels=levels,
            cmap=cm["cmap"],
            norm=mcolors.Normalize(vmin=cm["vmin"], vmax=cm["vmax"]),
            extend="both",
            antialiased=True,
            alpha=0.5,
        )
    except Exception as e:
        # contourf can fail on degenerate data — log and return empty
        logger.warning(f"contourf failed for {variable}: {e}")
        plt.close(fig)
        gc.collect()
        fig, ax = plt.subplots(figsize=(1, 1))
        fig.patch.set_alpha(0)
        ax.axis("off")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", transparent=True)
        plt.close(fig)
        gc.collect()
        buf.seek(0)
        return buf.getvalue()

    ax.set_xlim(UK_LON_MIN, UK_LON_MAX)
    ax.set_ylim(UK_LAT_MIN, UK_LAT_MAX)
    ax.set_aspect("auto")
    ax.axis("off")
    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)

    buf = io.BytesIO()
    fig.savefig(
        buf, format="png", dpi=dpi,
        bbox_inches="tight", pad_inches=0, transparent=True,
    )
    plt.close(fig)

    # CRITICAL: force garbage collection to reclaim matplotlib memory
    gc.collect()

    buf.seek(0)
    return buf.getvalue()
