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
from scipy.spatial import Delaunay, cKDTree

logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

INTERP_RESOLUTION = 200  # Default for overlays (lower = faster + less RAM)

# How far the surface may be drawn from a real observation, as a multiple of
# the grid's own median point spacing.
#
# A rate-limited run can leave whole latitude bands missing. Those voids sit
# inside the convex hull, so the interpolator happily spans them — and
# CloughTocher2D is a C1 *cubic*, unbounded by its inputs, fed unstable
# gradients from the sliver triangles a long gap produces. A live run with a
# 3 degree hole across Scotland rendered a saturated 100% chance of
# cancellation between two edges that both read 42%.
#
# Beyond this distance the surface is blanked, so a gap reads as "no data"
# rather than as a severe-weather signal. Expressed as a multiple of the
# measured spacing so it adapts to --resolution automatically.
MAX_GAP_FACTOR = 1.5

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
    # Chance of cancellation. Uses the same green/amber/red reading as the
    # verdict badges rather than "jet", so a colour on the map means the same
    # thing it means everywhere else in the portal.
    "pcancel": {"cmap": "RdYlGn_r", "vmin": 0,  "vmax": 100},
    "risk":   {"cmap": "jet",       "vmin": 0,  "vmax": 100},
    "wind":   {"cmap": "YlOrRd",    "vmin": 0,  "vmax": 25},
    "gust":   {"cmap": "YlOrRd",    "vmin": 0,  "vmax": 35},
    "precip": {"cmap": "Blues",      "vmin": 0,  "vmax": 8},
    "temp":   {"cmap": "RdYlBu_r",  "vmin": -5, "vmax": 25},
}


# ============================================================
# PROJECTION
# ============================================================
#
# The PNG this module renders is placed by L.imageOverlay, which stretches it
# linearly across the bounding box *in Web Mercator* — the projection Leaflet
# draws in. Sampling and plotting the surface linearly in latitude instead
# produced an equirectangular image, and the mismatch between the two moved
# every contour feature north: measured at +25 km through northern England,
# peaking at 26 km near 54.3N. Site markers are placed by Leaflet from their
# true coordinates, so the colour under a pin was the weather from ~25 km
# south of it, and the hover readout — which uses the stored grid coordinates
# — disagreed with the colour beneath the cursor.
#
# So the render grid is spaced evenly in Mercator y, and the figure's y axis
# is in Mercator y. Longitude needs no such treatment: Mercator is linear in
# longitude.

def mercator_y(lat_deg):
    """Web Mercator y for a latitude in degrees (unit sphere, no scaling)."""
    lat = np.radians(np.asarray(lat_deg, dtype=float))
    return np.log(np.tan(np.pi / 4.0 + lat / 2.0))


def inverse_mercator_y(y):
    """Latitude in degrees for a Web Mercator y."""
    y = np.asarray(y, dtype=float)
    return np.degrees(2.0 * np.arctan(np.exp(y)) - np.pi / 2.0)


# ============================================================
# INTERPOLATION
# ============================================================

def _median_point_spacing(points):
    """
    Median nearest-neighbour distance among the observations, in degrees.

    Returns None when it cannot be measured (fewer than two distinct points).
    """
    if len(points) < 2:
        return None

    tree = cKDTree(points)
    distances, _ = tree.query(points, k=2)   # k=2: the point itself, then its neighbour
    nearest = distances[:, 1]
    nearest = nearest[np.isfinite(nearest) & (nearest > 0)]

    if nearest.size == 0:
        return None
    return float(np.median(nearest))


def _blank_far_from_data(grid_lons, grid_lats, grid_values, points, max_distance):
    """Set cells further than max_distance from any observation to NaN."""
    tree = cKDTree(points)
    distances, _ = tree.query(
        np.column_stack([grid_lons.ravel(), grid_lats.ravel()])
    )
    too_far = distances.reshape(grid_values.shape) > max_distance

    blanked = grid_values.copy()
    blanked[too_far] = np.nan
    return blanked


def interpolate_risk_surface(lats, lons, values, resolution=INTERP_RESOLUTION,
                             max_distance=None):
    """
    Interpolate scattered data onto a regular grid using CloughTocher2D.

    The result is bounded by the observed values and blanked (NaN) wherever
    it would be drawn further than max_distance from any observation, so a
    gap in the input cannot be rendered as though it were measured.

    max_distance defaults to MAX_GAP_FACTOR times the grid's own median point
    spacing. Pass a value to override, or 0 to disable blanking entirely.

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
    # Rows evenly spaced in Mercator y, not in latitude, so that each row of
    # the rendered image lands on the latitude Leaflet will draw it at. The
    # returned latitudes are still true latitudes — only their spacing
    # changes — so distance-based blanking below is unaffected.
    grid_lat_1d = inverse_mercator_y(
        np.linspace(mercator_y(UK_LAT_MIN), mercator_y(UK_LAT_MAX), n_lat)
    )
    grid_lons, grid_lats = np.meshgrid(grid_lon_1d, grid_lat_1d)

    grid_pts = np.column_stack([grid_lons.ravel(), grid_lats.ravel()])
    grid_values = interpolator(grid_pts).reshape(grid_lons.shape)

    # Bound the surface by what was actually observed. Clipping later to the
    # colour scale's own range would hide a cubic overshoot rather than
    # prevent it — the overshoot would simply saturate at the top of the
    # scale, which is the most alarming colour on it.
    grid_values = np.clip(grid_values, float(values.min()), float(values.max()))

    if max_distance is None:
        spacing = _median_point_spacing(points)
        max_distance = spacing * MAX_GAP_FACTOR if spacing else 0

    if max_distance:
        grid_values = _blank_far_from_data(
            grid_lons, grid_lats, grid_values, points, max_distance
        )
        blanked = int(np.count_nonzero(np.isnan(grid_values)))
        if blanked:
            logger.info(
                "Interpolation: blanked %d of %d cells further than %.2f deg "
                "from any observation",
                blanked, grid_values.size, max_distance,
            )

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

    # Plot against Mercator y so the image matches how L.imageOverlay will
    # stretch it. See the projection note at the top of this module.
    grid_y = mercator_y(grid_lats)

    try:
        ax.contourf(
            grid_lons, grid_y, grid_values_masked,
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
    ax.set_ylim(float(mercator_y(UK_LAT_MIN)), float(mercator_y(UK_LAT_MAX)))
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
