"""
OrcaMet Portal — Dashboard Views

The main views a logged-in user sees.
"""

import json

from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from forecasts.models import ForecastRun, HourlyForecast, UKRiskMap, UKRiskGridRun, UKRiskGridPoint, CachedContourImage
from sites.models import Site


def _visible_sites(user):
    """Return the queryset of sites this user is allowed to see."""
    if user.is_superadmin:
        return Site.objects.filter(is_active=True).select_related("client")
    elif user.client:
        return Site.objects.filter(client=user.client, is_active=True).select_related("client")
    return Site.objects.none()


@login_required(login_url="/login/")
def home(request):
    """
    Main dashboard view.

    Superadmins see all sites.
    Client users see only their client's sites.
    """
    user = request.user
    sites_list = list(_visible_sites(user))

    for site in sites_list:
        site.latest_run = ForecastRun.objects.filter(site=site).order_by("-generated_at").first()

    generated_times = [s.latest_run.generated_at for s in sites_list if s.latest_run]
    alert_count = sum(1 for s in sites_list if s.latest_run and s.latest_run.status == ForecastRun.Status.SUCCESS and s.latest_run.recommendation in ("CAUTION", "CANCEL"))

    context = {"user": user, "sites": sites_list, "site_count": len(sites_list), "latest_forecast_at": max(generated_times) if generated_times else None, "alert_count": alert_count, "uk_risk_map": UKRiskMap.objects.order_by("-generated_at").first()}

    return render(request, "dashboard/home.html", context)


@login_required(login_url="/login/")
def site_detail(request, site_id):
    """
    Detail view for a single site.

    Shows a multi-day forecast summary, hourly charts, active thresholds,
    and an hourly breakdown table for one site.
    """
    user = request.user

    if user.is_superadmin:
        site = get_object_or_404(Site, pk=site_id, is_active=True)
    elif user.client:
        site = get_object_or_404(Site, pk=site_id, client=user.client, is_active=True)
    else:
        raise Http404("Site not found")

    today = timezone.localdate()

    latest_ids = ForecastRun.objects.filter(site=site, status=ForecastRun.Status.SUCCESS, forecast_date__gte=today).values("forecast_date").annotate(latest_id=Max("id")).values_list("latest_id", flat=True)
    forecast_days = list(ForecastRun.objects.filter(id__in=latest_ids).order_by("forecast_date"))

    threshold = site.thresholds.filter(is_active=True).first()

    if threshold:
        thresholds_dict = threshold.as_dict()
    else:
        thresholds_dict = {"wind_mean_caution": 10.0, "wind_mean_cancel": 14.0, "gust_caution": 15.0, "gust_cancel": 20.0, "precip_caution": 0.7, "precip_cancel": 2.0, "temp_min_caution": 1.0, "temp_min_cancel": -2.0}

    hourly_qs = HourlyForecast.objects.filter(run__in=forecast_days).order_by("timestamp")
    hourly_list = [{"time": h.timestamp.isoformat(), "risk": h.hourly_risk, "wind_speed": h.wind_speed, "wind_gusts": h.wind_gusts, "precipitation": h.precipitation, "temperature": h.temperature} for h in hourly_qs]

    chart_data = {"hourly": hourly_list, "thresholds": thresholds_dict, "debug": {"run_ids": [r.id for r in forecast_days], "hourly_count": len(hourly_list)}}

    context = {"user": user, "site": site, "forecast_days": forecast_days, "today": today, "threshold": threshold, "chart_data_json": json.dumps(chart_data, default=str)}

    return render(request, "dashboard/site_detail.html", context)


@login_required(login_url="/login/")
def weather_map(request):
    """UK-wide interactive weather and risk map."""
    user = request.user
    sites_qs = _visible_sites(user)

    recommendations = []
    for site in sites_qs:
        run = ForecastRun.objects.filter(site=site, status=ForecastRun.Status.SUCCESS).order_by("-generated_at").first()
        if run and run.recommendation:
            recommendations.append(run.recommendation)

    latest_grid_run = UKRiskGridRun.objects.filter(status=UKRiskGridRun.Status.SUCCESS).order_by("-generated_at").first()

    data_age_hours = None
    last_grid_update = None
    if latest_grid_run:
        last_grid_update = latest_grid_run.generated_at
        delta = timezone.now() - last_grid_update
        data_age_hours = round(delta.total_seconds() / 3600, 1)

    context = {"user": user, "data_age_hours": data_age_hours, "last_grid_update": last_grid_update, "go_count": recommendations.count("GO"), "caution_count": recommendations.count("CAUTION"), "cancel_count": recommendations.count("CANCEL")}

    return render(request, "dashboard/weather_map.html", context)


@login_required(login_url="/login/")
def map_sites_json(request):
    """GeoJSON of all visible sites with their latest forecast summary."""
    user = request.user
    sites_qs = _visible_sites(user)

    features = []
    for site in sites_qs:
        if not site.coords:
            continue

        run = ForecastRun.objects.filter(site=site, status=ForecastRun.Status.SUCCESS).order_by("-generated_at").first()

        props = {"id": site.id, "name": site.name, "client": site.client.name, "postcode": site.postcode, "exposure": site.get_exposure_display(), "has_forecast": bool(run), "recommendation": run.recommendation if run else None, "peak_risk": run.peak_risk if run else None, "peak_wind": run.peak_wind if run else None, "peak_gust": run.peak_gust if run else None, "peak_precip": run.peak_precip if run else None, "min_temp": run.min_temp if run else None, "forecast_date": run.forecast_date.isoformat() if run else None}
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [site.longitude, site.latitude]}, "properties": props})


    return JsonResponse({"type": "FeatureCollection", "features": features})


@login_required(login_url="/login/")
def map_sites_hourly_json(request):
    """Per-timestamp GeoJSON frames of hourly data for all visible sites."""
    user = request.user
    sites_qs = _visible_sites(user)

    site_hours = []
    timestamps_set = set()

    for site in sites_qs:
        if not site.coords:
            continue

        run = ForecastRun.objects.filter(site=site, status=ForecastRun.Status.SUCCESS).order_by("-generated_at").first()
        if not run:
            continue

        hours = list(run.hourly_data.all().order_by("timestamp")[:48])
        site_hours.append((site, hours))
        for h in hours:
            timestamps_set.add(h.timestamp)

    timestamps = sorted(timestamps_set)
    ts_strs = [t.isoformat() for t in timestamps]
    frames = {ts: {"features": []} for ts in ts_strs}

    for site, hours in site_hours:
        for h in hours:
            ts_str = h.timestamp.isoformat()
            frames[ts_str]["features"].append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [site.longitude, site.latitude]}, "properties": {"id": site.id, "name": site.name, "client": site.client.name, "postcode": site.postcode, "wind_speed": h.wind_speed, "wind_gusts": h.wind_gusts, "precipitation": h.precipitation, "temperature": h.temperature}})

    return JsonResponse({"timestamps": ts_strs, "frames": frames})


@login_required(login_url="/login/")
def map_contour_image(request):
    """Serve a single cached contour PNG for a variable/timestamp."""
    var_name = request.GET.get("var", "risk")
    timestamp = request.GET.get("timestamp")

    run = UKRiskGridRun.objects.filter(status=UKRiskGridRun.Status.SUCCESS).order_by("-generated_at").first()
    if not run:
        raise Http404("No grid run available")

    images = CachedContourImage.objects.filter(run=run, variable=var_name)
    image = None

    if timestamp:
        parsed = parse_datetime(timestamp)
        if parsed:
            image = images.filter(timestamp=parsed).first()

    if not image:
        image = images.order_by("timestamp").first()

    if not image:
        raise Http404("No contour image available")

    return HttpResponse(bytes(image.image_data), content_type="image/png")


@login_required(login_url="/login/")
def map_contour_timestamps(request):
    """List available contour timestamps for the latest UK risk grid run."""
    run = UKRiskGridRun.objects.filter(status=UKRiskGridRun.Status.SUCCESS).order_by("-generated_at").first()

    if not run:
        return JsonResponse({"available": False, "timestamps": [], "has_cache": False})

    timestamps = list(UKRiskGridPoint.objects.filter(run=run).values_list("timestamp", flat=True).distinct().order_by("timestamp"))
    has_cache = CachedContourImage.objects.filter(run=run).exists()

    return JsonResponse({"available": True, "timestamps": [t.isoformat() for t in timestamps], "has_cache": has_cache, "models_used": run.models_used, "grid_points": run.grid_points, "run_key": str(run.id), "generated_at": run.generated_at.isoformat()})
