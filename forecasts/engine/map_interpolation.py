"""
OrcaMet Portal — Map Interpolation Engine

Generates smooth, coastline-clipped contour maps of weather variables
across the UK using CloughTocher2D interpolation (C1-continuous,
Akima-style).

Public API:
    generate_uk_risk_map()       — Full map with chrome (title, colourbar, branding)
    render_contour_to_bytes()    — Transparent PNG bytes for L.imageOverlay (no chrome)
    interpolate_risk_surface()   — Raw interpolation (returns grid arrays)
    generate_map_from_grid_run() — Convenience: generate from DB records
"""

import gc
import io
import base64
import logging
from pathlib import Path

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

# Figure size for full standalone maps (with chrome)
FULL_FIG_WIDTH = 8
FULL_FIG_HEIGHT = 12
FULL_FIG_DPI = 150

# Contour levels for risk percentage (0–100%)
CONTOUR_LEVELS = np.linspace(0, 100, 51)

# Variable-specific colour map configuration
VARIABLE_CMAPS = {
    "risk":   {"cmap": "jet",       "vmin": 0,  "vmax": 100},
    "wind":   {"cmap": "YlOrRd",    "vmin": 0,  "vmax": 25},
    "gust":   {"cmap": "YlOrRd",    "vmin": 0,  "vmax": 35},
    "precip": {"cmap": "Blues",      "vmin": 0,  "vmax": 8},
    "temp":   {"cmap": "RdYlBu_r",  "vmin": -5, "vmax": 25},
}


# ============================================================
# COASTLINE LOADING
# ============================================================

_land_geometry_cache = None
_land_geometry_loaded = False


def _get_uk_land_geometry():
    """
    Load UK coastline geometry for land-masking.
    Cached at module level — only loads once per process.
    """
    global _land_geometry_cache, _land_geometry_loaded

    if _land_geometry_loaded:
        return _land_geometry_cache

    _land_geometry_loaded = True

    try:
        import geopandas as gpd
        from shapely.ops import unary_union
        from shapely.geometry import box

        world = None

        # Strategy 1: Bundled dataset (geopandas < 1.0)
        try:
            world = gpd.read_file(gpd.datasets.get_path("naturalearth_lowres"))
            logger.info("Loaded coastline from bundled naturalearth_lowres")
        except Exception:
            pass

        # Strategy 2: Download from CDN
        if world is None:
            try:
                import urllib.request
                import tempfile
                import os

                ne_url = (
                    "https://naciscdn.org/naturalearth/110m/cultural/"
                    "ne_110m_admin_0_countries.zip"
                )
                logger.info("Downloading Natural Earth data...")

                with tempfile.TemporaryDirectory() as tmpdir:
                    zip_path = os.path.join(tmpdir, "ne.zip")
                    urllib.request.urlretrieve(ne_url, zip_path)
                    if os.path.getsize(zip_path) > 0:
                        world = gpd.read_file(f"zip://{zip_path}")
                        logger.info("Loaded coastline from Natural Earth CDN")
            except Exception as e:
                logger.warning(f"Natural Earth download failed: {e}")

        if world is None:
            return None

        # Detect column names
        name_col = None
        iso_col = None
        for col in ["name", "NAME", "NAME_EN", "ADMIN", "admin"]:
            if col in world.columns:
                name_col = col
                break
        for col in ["iso_a3", "ISO_A3", "ISO_A3_EH", "ADM0_A3"]:
            if col in world.columns:
                iso_col = col
                break

        uk_geom = None
        if name_col:
            uk_geom = world[world[name_col].isin(["United Kingdom", "Ireland"])]
        if (uk_geom is None or uk_geom.empty) and iso_col:
            uk_geom = world[world[iso_col].isin(["GBR", "IRL"])]

        if uk_geom is None or uk_geom.empty:
            logger.warning(f"Could not find UK/Ireland. Columns: {list(world.columns)}")
            return None

        land = unary_union(uk_geom.geometry)
        bbox = box(UK_LON_MIN - 0.5, UK_LAT_MIN - 0.5, UK_LON_MAX + 0.5, UK_LAT_MAX + 0.5)
        land = land.intersection(bbox)

        logger.info(f"Loaded UK coastline geometry ({land.geom_type})")
        _land_geometry_cache = land
        return land

    except ImportError as e:
        logger.warning(f"geopandas/shapely not available: {e}")
    except Exception as e:
        logger.warning(f"Failed to load coastline: {e}")

    return None


def _create_land_mask(land_geom, grid_lons, grid_lats):
    if land_geom is None:
        return np.ones(grid_lons.shape, dtype=bool)
    try:
        from shapely.vectorized import contains
        return contains(land_geom, grid_lons, grid_lats)
    except ImportError:
        from shapely.geometry import Point
        mask = np.zeros(grid_lons.shape, dtype=bool)
        for i in range(grid_lons.shape[0]):
            for j in range(grid_lons.shape[1]):
                mask[i, j] = land_geom.contains(Point(grid_lons[i, j], grid_lats[i, j]))
        return mask


def _draw_coastline(ax, land_geom):
    try:
        from shapely.geometry import MultiPolygon, Polygon
        geoms = list(land_geom.geoms) if isinstance(land_geom, MultiPolygon) else [land_geom]
        for poly in geoms:
            x, y = poly.exterior.xy
            ax.plot(x, y, color="white", linewidth=0.5, alpha=0.5)
            for interior in poly.interiors:
                x, y = interior.xy
                ax.plot(x, y, color="white", linewidth=0.3, alpha=0.4)
    except Exception as e:
        logger.warning(f"Failed to draw coastline: {e}")


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
    No axes, no chrome — just the contour fill + coastline.

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


# ============================================================
# FULL MAP RENDERING (with chrome — title, colourbar, branding)
# ============================================================

def generate_uk_risk_map(
    lats: np.ndarray, lons: np.ndarray, risks: np.ndarray,
    resolution: int = 300, title: str = None, forecast_date: str = None,
) -> str:
    """Generate a full risk map with chrome. Returns base64 PNG."""
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    risks = np.asarray(risks, dtype=float)

    grid_lons, grid_lats, grid_values = interpolate_risk_surface(
        lats, lons, risks, resolution=resolution
    )

    land_geom = _get_uk_land_geometry()
    land_mask = _create_land_mask(land_geom, grid_lons, grid_lats)
    grid_values_masked = np.where(land_mask, grid_values, np.nan)
    grid_values_masked = np.clip(grid_values_masked, 0, 100)

    fig, ax = plt.subplots(figsize=(FULL_FIG_WIDTH, FULL_FIG_HEIGHT), facecolor="#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    cf = ax.contourf(
        grid_lons, grid_lats, grid_values_masked,
        levels=CONTOUR_LEVELS, cmap=plt.cm.jet,
        norm=mcolors.Normalize(vmin=0, vmax=100),
        extend="both", antialiased=True,
    )

    if land_geom is not None:
        _draw_coastline(ax, land_geom)

    ax.set_xlim(UK_LON_MIN, UK_LON_MAX)
    ax.set_ylim(UK_LAT_MIN, UK_LAT_MAX)
    ax.set_aspect("auto")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    cbar = fig.colorbar(cf, ax=ax, orientation="horizontal", fraction=0.04, pad=0.02, aspect=40)
    cbar.set_label("Risk (%)", color="white", fontsize=11)
    cbar.ax.tick_params(colors="white", labelsize=9)
    cbar.set_ticks([0, 20, 40, 60, 80, 100])
    cbar.set_ticklabels(["0%\nGO", "20%", "40%", "60%", "80%", "100%\nCANCEL"])

    ax.set_title(title or "UK Construction Risk Map", color="white", fontsize=16, fontweight="bold", pad=12)
    if forecast_date:
        ax.text(0.5, 1.01, forecast_date, transform=ax.transAxes, ha="center", va="bottom", color="#aaaaaa", fontsize=10)
    ax.text(0.99, 0.01, "OrcaMet", transform=ax.transAxes, ha="right", va="bottom", color="#ffffff88", fontsize=9, fontstyle="italic")

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=FULL_FIG_DPI, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    gc.collect()
    buf.seek(0)

    return base64.b64encode(buf.read()).decode("utf-8")


# ============================================================
# CONVENIENCE: Generate from database records
# ============================================================

def generate_map_from_grid_run(grid_run_id: int, resolution: int = 300) -> str:
    from forecasts.models import UKRiskGridRun, UKRiskGridPoint
    from django.db.models import Max

    grid_run = UKRiskGridRun.objects.get(pk=grid_run_id)
    points = UKRiskGridPoint.objects.filter(run=grid_run)
    if not points.exists():
        raise ValueError(f"No grid points found for run {grid_run_id}")

    peak_risks = points.values("latitude", "longitude").annotate(peak_risk=Max("risk"))
    lats = np.array([p["latitude"] for p in peak_risks])
    lons = np.array([p["longitude"] for p in peak_risks])
    risks = np.array([p["peak_risk"] for p in peak_risks])

    return generate_uk_risk_map(
        lats, lons, risks, resolution=resolution,
        title="UK Construction Risk — Peak Exceedance",
        forecast_date=grid_run.forecast_date.strftime("%A %d %B %Y"),
    )
