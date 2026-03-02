# ============================================================
# ADD THESE TO dashboard/views.py
# ============================================================
# 
# 1. Add this import at the top of the file:
#
#    import base64
#    from django.http import HttpResponse
#
# 2. Add these two view functions at the bottom of the file:


@login_required(login_url="/login/")
def map_contour_image(request):
    """
    Serve a CloughTocher2D contour map as a PNG image.

    Query params:
        hour (int, optional): UTC hour (0-23). If omitted, returns peak risk map.
        var (str, optional): Variable to map — 'risk', 'wind', 'gust', 'precip', 'temp'.
                             Default: 'risk'.
        resolution (int, optional): Interpolation resolution. Default: 300.

    Returns PNG image directly (not JSON, not base64).
    """
    from forecasts.models import UKRiskGridRun, UKRiskGridPoint
    from django.db.models import Max, Min
    import numpy as np

    try:
        from forecasts.engine.map_interpolation import (
            interpolate_risk_surface,
            _get_uk_land_geometry,
            _create_land_mask,
            UK_LAT_MIN, UK_LAT_MAX, UK_LON_MIN, UK_LON_MAX,
        )
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.colors as mcolors
        import io
    except ImportError as e:
        return HttpResponse(
            f"Map dependencies not installed: {e}",
            status=500, content_type="text/plain",
        )

    # Find latest successful grid run
    grid_run = (
        UKRiskGridRun.objects
        .filter(status=UKRiskGridRun.Status.SUCCESS)
        .order_by("-generated_at")
        .first()
    )
    if not grid_run:
        return HttpResponse("No grid data available", status=404, content_type="text/plain")

    target_hour = request.GET.get("hour")
    var_name = request.GET.get("var", "risk")
    resolution = int(request.GET.get("resolution", "300"))

    points_qs = UKRiskGridPoint.objects.filter(run=grid_run)

    if target_hour is not None:
        target_hour = int(target_hour)
        points_qs = points_qs.filter(timestamp__hour=target_hour)

        if not points_qs.exists():
            return HttpResponse("No data for this hour", status=404, content_type="text/plain")

        lats = np.array(list(points_qs.values_list("latitude", flat=True)))
        lons = np.array(list(points_qs.values_list("longitude", flat=True)))

        # Select the variable to map
        if var_name == "wind":
            values = np.array(list(points_qs.values_list("wind_speed", flat=True)))
        elif var_name == "gust":
            values = np.array(list(points_qs.values_list("wind_gusts", flat=True)))
        elif var_name == "precip":
            values = np.array(list(points_qs.values_list("precipitation", flat=True)))
        elif var_name == "temp":
            values = np.array(list(points_qs.values_list("temperature", flat=True)))
        else:
            values = np.array(list(points_qs.values_list("risk", flat=True)))
    else:
        # Peak across all hours
        if var_name == "wind":
            agg = points_qs.values("latitude", "longitude").annotate(val=Max("wind_speed"))
        elif var_name == "gust":
            agg = points_qs.values("latitude", "longitude").annotate(val=Max("wind_gusts"))
        elif var_name == "precip":
            agg = points_qs.values("latitude", "longitude").annotate(val=Max("precipitation"))
        elif var_name == "temp":
            agg = points_qs.values("latitude", "longitude").annotate(val=Min("temperature"))
        else:
            agg = points_qs.values("latitude", "longitude").annotate(val=Max("risk"))

        lats = np.array([p["latitude"] for p in agg])
        lons = np.array([p["longitude"] for p in agg])
        values = np.array([p["val"] for p in agg])

    if len(lats) < 4:
        return HttpResponse("Not enough data points", status=404, content_type="text/plain")

    # Interpolate
    grid_lons, grid_lats, grid_values = interpolate_risk_surface(
        lats, lons, values, resolution=resolution,
    )

    # Land mask
    land_geom = _get_uk_land_geometry()
    land_mask = _create_land_mask(land_geom, grid_lons, grid_lats)
    grid_values_masked = np.where(land_mask, grid_values, np.nan)

    # Colour mapping per variable
    CMAPS = {
        "risk": {"cmap": "jet", "vmin": 0, "vmax": 100},
        "wind": {"cmap": "YlOrRd", "vmin": 0, "vmax": 25},
        "gust": {"cmap": "YlOrRd", "vmin": 0, "vmax": 35},
        "precip": {"cmap": "Blues", "vmin": 0, "vmax": 8},
        "temp": {"cmap": "RdYlBu_r", "vmin": -5, "vmax": 25},
    }
    cm = CMAPS.get(var_name, CMAPS["risk"])

    # Render — transparent background, no axes, no chrome
    fig, ax = plt.subplots(figsize=(8, 12), dpi=150)
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")

    levels = np.linspace(cm["vmin"], cm["vmax"], 51)
    cf = ax.contourf(
        grid_lons, grid_lats, grid_values_masked,
        levels=levels,
        cmap=cm["cmap"],
        norm=mcolors.Normalize(vmin=cm["vmin"], vmax=cm["vmax"]),
        extend="both",
        antialiased=True,
        alpha=0.7,
    )

    # Coastline outline
    if land_geom is not None:
        try:
            from shapely.geometry import MultiPolygon, Polygon
            geoms = list(land_geom.geoms) if isinstance(land_geom, MultiPolygon) else [land_geom]
            for poly in geoms:
                x, y = poly.exterior.xy
                ax.plot(x, y, color="white", linewidth=0.5, alpha=0.5)
        except Exception:
            pass

    ax.set_xlim(UK_LON_MIN, UK_LON_MAX)
    ax.set_ylim(UK_LAT_MIN, UK_LAT_MAX)
    ax.set_aspect("auto")
    ax.axis("off")
    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                pad_inches=0, transparent=True)
    plt.close(fig)
    buf.seek(0)

    response = HttpResponse(buf.getvalue(), content_type="image/png")
    response["Cache-Control"] = "public, max-age=300"
    return response


@login_required(login_url="/login/")
def map_contour_timestamps(request):
    """
    JSON endpoint returning available timestamps for the contour map time slider.
    """
    from forecasts.models import UKRiskGridRun, UKRiskGridPoint

    grid_run = (
        UKRiskGridRun.objects
        .filter(status=UKRiskGridRun.Status.SUCCESS)
        .order_by("-generated_at")
        .first()
    )
    if not grid_run:
        return JsonResponse({"available": False})

    timestamps = list(
        UKRiskGridPoint.objects.filter(run=grid_run)
        .values_list("timestamp", flat=True)
        .distinct()
        .order_by("timestamp")
    )

    return JsonResponse({
        "available": True,
        "forecast_date": grid_run.forecast_date.isoformat(),
        "generated_at": grid_run.generated_at.isoformat(),
        "grid_points": grid_run.grid_points,
        "timestamps": [t.isoformat() if hasattr(t, 'isoformat') else str(t) for t in timestamps],
        "hours": [t.hour if hasattr(t, 'hour') else 0 for t in timestamps],
    })
