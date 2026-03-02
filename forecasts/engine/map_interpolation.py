"""
OrcaMet Portal — Map Interpolation Engine

Generates smooth, coastline-clipped contour maps of threshold exceedance
probability across the UK using CloughTocher2D interpolation (C1-continuous,
Akima-style).

Replaces the previous scipy.griddata approach with higher-quality interpolation
that produces smooth, gridless contour fills clipped to the UK land boundary.

Usage (standalone):
    from forecasts.engine.map_interpolation import generate_uk_risk_map
    png_base64 = generate_uk_risk_map(lats, lons, risks)

Usage (from management command):
    python manage.py generate_risk_map
"""

import io
import base64
import logging
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for server rendering
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.patches import FancyBboxPatch
from scipy.interpolate import CloughTocher2DInterpolator
from scipy.spatial import Delaunay

logger = logging.getLogger(__name__)

# ============================================================
# CONFIGURATION
# ============================================================

# Interpolation resolution: number of grid points along the longest axis.
# 300 = fast (~1s), 500 = detailed (~3s), 800 = publication (~8s), 1000 = max
INTERP_RESOLUTION = 300

# UK bounding box (matches risk_grid.py)
UK_LAT_MIN = 49.9
UK_LAT_MAX = 58.7
UK_LON_MIN = -7.6
UK_LON_MAX = 1.8

# Map figure size (inches) and DPI
FIGURE_WIDTH = 8
FIGURE_HEIGHT = 12
FIGURE_DPI = 150

# Contour levels for risk percentage (0–100%)
CONTOUR_LEVELS = np.linspace(0, 100, 51)  # 2% steps for smooth gradients


# ============================================================
# COASTLINE LOADING
# ============================================================

def _get_uk_land_geometry():
    """
    Load UK coastline geometry for land-masking.

    Uses Natural Earth 1:50m cultural boundaries (admin-0 countries),
    filtered to UK + Ireland land areas. Falls back to a simplified
    bounding polygon if geopandas/shapely aren't available or the
    shapefile can't be loaded.

    Returns a shapely geometry (MultiPolygon) of UK land areas.
    """
    try:
        import geopandas as gpd
        from shapely.ops import unary_union
        from shapely.geometry import box

        # Try Natural Earth dataset bundled with geopandas
        try:
            world = gpd.read_file(
                gpd.datasets.get_path("naturalearth_lowres")
            )
        except Exception:
            # geopandas >= 1.0 removed datasets module; use built-in
            world = gpd.read_file(
                "https://naciscdn.org/naturalearth/110m/cultural/"
                "ne_110m_admin_0_countries.zip"
            )

        # Filter to UK and Ireland (Ireland included to mask its land area)
        uk_names = [
            "United Kingdom",
            "Ireland",
        ]
        uk_geom = world[world["name"].isin(uk_names)]

        if uk_geom.empty:
            # Fallback: try ISO codes
            uk_geom = world[world["iso_a3"].isin(["GBR", "IRL"])]

        if not uk_geom.empty:
            land = unary_union(uk_geom.geometry)
            # Clip to our bounding box
            bbox = box(UK_LON_MIN - 0.5, UK_LAT_MIN - 0.5,
                       UK_LON_MAX + 0.5, UK_LAT_MAX + 0.5)
            land = land.intersection(bbox)
            logger.info(f"Loaded UK coastline geometry ({land.geom_type})")
            return land

    except ImportError as e:
        logger.warning(f"geopandas/shapely not available: {e}")
    except Exception as e:
        logger.warning(f"Failed to load coastline: {e}")

    logger.warning("Using fallback UK bounding polygon (no coastline clip)")
    return None


def _create_land_mask(land_geom, grid_lons, grid_lats):
    """
    Create a boolean mask where True = land, False = sea.

    Parameters
    ----------
    land_geom : shapely geometry or None
        UK land polygon. If None, returns all-True mask (no clipping).
    grid_lons : 2D ndarray
        Longitude grid (meshgrid output).
    grid_lats : 2D ndarray
        Latitude grid (meshgrid output).

    Returns
    -------
    mask : 2D boolean ndarray
        True where the grid point is over land.
    """
    if land_geom is None:
        return np.ones(grid_lons.shape, dtype=bool)

    try:
        from shapely.geometry import Point
        from shapely.vectorized import contains
        # Fast vectorised containment check
        mask = contains(land_geom, grid_lons, grid_lats)
        return mask
    except ImportError:
        # Fallback: point-by-point (slower but works)
        from shapely.geometry import Point
        mask = np.zeros(grid_lons.shape, dtype=bool)
        for i in range(grid_lons.shape[0]):
            for j in range(grid_lons.shape[1]):
                mask[i, j] = land_geom.contains(
                    Point(grid_lons[i, j], grid_lats[i, j])
                )
        return mask


# ============================================================
# INTERPOLATION
# ============================================================

def interpolate_risk_surface(
    lats: np.ndarray,
    lons: np.ndarray,
    values: np.ndarray,
    resolution: int = INTERP_RESOLUTION,
) -> tuple:
    """
    Interpolate scattered (lat, lon, value) data onto a regular grid
    using CloughTocher2D (C1-continuous piecewise cubic interpolation
    based on Delaunay triangulation).

    Parameters
    ----------
    lats : 1D array of latitudes
    lons : 1D array of longitudes
    values : 1D array of risk values (0–100)
    resolution : int
        Number of grid points along the longest axis.

    Returns
    -------
    grid_lons : 2D ndarray
    grid_lats : 2D ndarray
    grid_values : 2D ndarray (NaN outside convex hull)
    """
    # Remove any NaN input points
    valid = ~(np.isnan(lats) | np.isnan(lons) | np.isnan(values))
    lats = lats[valid]
    lons = lons[valid]
    values = values[valid]

    if len(lats) < 4:
        raise ValueError(
            f"Need at least 4 data points for interpolation, got {len(lats)}"
        )

    # Build the interpolator
    points = np.column_stack([lons, lats])

    try:
        tri = Delaunay(points)
    except Exception as e:
        raise ValueError(f"Delaunay triangulation failed: {e}")

    interpolator = CloughTocher2DInterpolator(tri, values, tol=1e-6)

    # Create the regular output grid
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

    # Evaluate the interpolator on the grid
    grid_points = np.column_stack([grid_lons.ravel(), grid_lats.ravel()])
    grid_values = interpolator(grid_points).reshape(grid_lons.shape)

    # Clamp to valid range
    grid_values = np.clip(grid_values, 0, 100)

    logger.info(
        f"Interpolated {len(lats)} points → {n_lon}×{n_lat} grid "
        f"(resolution={resolution})"
    )

    return grid_lons, grid_lats, grid_values


# ============================================================
# MAP RENDERING
# ============================================================

def generate_uk_risk_map(
    lats: np.ndarray,
    lons: np.ndarray,
    risks: np.ndarray,
    resolution: int = INTERP_RESOLUTION,
    title: str = None,
    forecast_date: str = None,
) -> str:
    """
    Generate a smooth contour map of UK risk, clipped to coastline.

    Parameters
    ----------
    lats : 1D array of point latitudes
    lons : 1D array of point longitudes
    risks : 1D array of risk values (0–100%)
    resolution : int
        Interpolation grid resolution.
    title : str, optional
        Map title. Defaults to "UK Construction Risk Map".
    forecast_date : str, optional
        Date string to show in subtitle.

    Returns
    -------
    base64_png : str
        Base64-encoded PNG image of the map.
    """
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    risks = np.asarray(risks, dtype=float)

    # Step 1: Interpolate to regular grid
    grid_lons, grid_lats, grid_values = interpolate_risk_surface(
        lats, lons, risks, resolution=resolution
    )

    # Step 2: Load coastline and create land mask
    land_geom = _get_uk_land_geometry()
    land_mask = _create_land_mask(land_geom, grid_lons, grid_lats)

    # Mask sea areas with NaN (so contourf won't draw there)
    grid_values_masked = np.where(land_mask, grid_values, np.nan)

    # Step 3: Render the map
    fig, ax = plt.subplots(
        figsize=(FIGURE_WIDTH, FIGURE_HEIGHT),
        facecolor="#1a1a2e",
    )
    ax.set_facecolor("#1a1a2e")

    # Jet colormap for risk (0=blue/safe → 100=red/dangerous)
    cmap = plt.cm.jet
    norm = mcolors.Normalize(vmin=0, vmax=100)

    # Filled contours — the main visual layer
    cf = ax.contourf(
        grid_lons,
        grid_lats,
        grid_values_masked,
        levels=CONTOUR_LEVELS,
        cmap=cmap,
        norm=norm,
        extend="both",
        antialiased=True,
    )

    # Draw coastline outline on top
    if land_geom is not None:
        _draw_coastline(ax, land_geom)

    # Remove all axes, ticks, grid lines, spines — clean map only
    ax.set_xlim(UK_LON_MIN, UK_LON_MAX)
    ax.set_ylim(UK_LAT_MIN, UK_LAT_MAX)
    ax.set_aspect("auto")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    # Colourbar
    cbar = fig.colorbar(
        cf, ax=ax, orientation="horizontal",
        fraction=0.04, pad=0.02, aspect=40,
    )
    cbar.set_label("Risk (%)", color="white", fontsize=11)
    cbar.ax.tick_params(colors="white", labelsize=9)
    cbar.set_ticks([0, 20, 40, 60, 80, 100])
    cbar.set_ticklabels(["0%\nGO", "20%", "40%", "60%", "80%", "100%\nCANCEL"])

    # Title
    if title is None:
        title = "UK Construction Risk Map"
    ax.set_title(title, color="white", fontsize=16, fontweight="bold", pad=12)

    if forecast_date:
        ax.text(
            0.5, 1.01, forecast_date,
            transform=ax.transAxes,
            ha="center", va="bottom",
            color="#aaaaaa", fontsize=10,
        )

    # OrcaMet branding
    ax.text(
        0.99, 0.01, "OrcaMet",
        transform=ax.transAxes,
        ha="right", va="bottom",
        color="#ffffff88", fontsize=9, fontstyle="italic",
    )

    plt.tight_layout()

    # Step 4: Export to base64 PNG
    buf = io.BytesIO()
    fig.savefig(
        buf, format="png", dpi=FIGURE_DPI,
        bbox_inches="tight", facecolor=fig.get_facecolor(),
    )
    plt.close(fig)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("utf-8")

    logger.info(
        f"Generated risk map: {len(lats)} points, "
        f"resolution={resolution}, "
        f"image size={len(b64) // 1024}KB"
    )

    return b64


def _draw_coastline(ax, land_geom):
    """Draw the UK coastline outline on the map axes."""
    try:
        from shapely.geometry import MultiPolygon, Polygon

        if isinstance(land_geom, Polygon):
            geoms = [land_geom]
        elif isinstance(land_geom, MultiPolygon):
            geoms = list(land_geom.geoms)
        else:
            return

        for poly in geoms:
            # Exterior ring
            x, y = poly.exterior.xy
            ax.plot(x, y, color="white", linewidth=0.6, alpha=0.7)
            # Interior rings (islands/lakes)
            for interior in poly.interiors:
                x, y = interior.xy
                ax.plot(x, y, color="white", linewidth=0.4, alpha=0.5)

    except Exception as e:
        logger.warning(f"Failed to draw coastline: {e}")


# ============================================================
# CONVENIENCE: Generate from database records
# ============================================================

def generate_map_from_grid_run(grid_run_id: int, resolution: int = INTERP_RESOLUTION) -> str:
    """
    Generate a risk map from stored UKRiskGridRun data.

    Parameters
    ----------
    grid_run_id : int
        Primary key of the UKRiskGridRun record.
    resolution : int
        Interpolation resolution.

    Returns
    -------
    base64_png : str
        Base64-encoded PNG.
    """
    from forecasts.models import UKRiskGridRun, UKRiskGridPoint

    grid_run = UKRiskGridRun.objects.get(pk=grid_run_id)
    points = UKRiskGridPoint.objects.filter(run=grid_run)

    if not points.exists():
        raise ValueError(f"No grid points found for run {grid_run_id}")

    # Aggregate: peak risk per (lat, lon) across all timestamps
    from django.db.models import Max
    peak_risks = (
        points
        .values("latitude", "longitude")
        .annotate(peak_risk=Max("risk"))
    )

    lats = np.array([p["latitude"] for p in peak_risks])
    lons = np.array([p["longitude"] for p in peak_risks])
    risks = np.array([p["peak_risk"] for p in peak_risks])

    forecast_date = grid_run.forecast_date.strftime("%A %d %B %Y")

    return generate_uk_risk_map(
        lats, lons, risks,
        resolution=resolution,
        title="UK Construction Risk — Peak Exceedance",
        forecast_date=forecast_date,
    )
