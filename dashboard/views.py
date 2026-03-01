"""
OrcaMet Portal — Dashboard Views

Main views for logged-in users: dashboard overview and site detail with
live forecast data, charts, and risk assessments.
"""

import json
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone

from forecasts.models import ForecastRun, HourlyForecast
from sites.models import Site


def _get_user_sites(user):
    """Return the queryset of sites visible to a user."""
    if user.is_superadmin:
        return Site.objects.filter(is_active=True).select_related("client")
    elif user.client:
        return Site.objects.filter(
            client=user.client, is_active=True
        ).select_related("client")
    return Site.objects.none()


def _latest_run_for_site(site):
    """Return the most recent successful ForecastRun for a site."""
    return (
        ForecastRun.objects.filter(site=site, status=ForecastRun.Status.SUCCESS)
        .order_by("-forecast_date", "-generated_at")
        .first()
    )


def _annotate_sites_with_forecasts(sites_qs):
    """Attach latest forecast info to each site object for template use."""
    annotated = []
    today = date.today()

    for site in sites_qs:
        run = (
            ForecastRun.objects.filter(
                site=site, status=ForecastRun.Status.SUCCESS, forecast_date=today
            )
            .order_by("-generated_at")
            .first()
        )
        if run is None:
            run = _latest_run_for_site(site)

        site.latest_run = run
        annotated.append(site)

    return annotated


def _build_chart_data(site, forecast_days):
    """
    Build the hourly chart data as a Python dict, ready to be serialised
    into the template as inline JSON. This eliminates the need for a
    separate AJAX fetch() call.
    """
    from sites.models import ThresholdProfile

    if not forecast_days:
        return json.dumps({"hourly": [], "thresholds": {}})

    run_ids = [run.pk for run in forecast_days]

    hourly_qs = (
        HourlyForecast.objects.filter(run_id__in=run_ids)
        .order_by("timestamp")
        .values(
            "timestamp",
            "wind_speed",
            "wind_gusts",
            "precipitation",
            "temperature",
            "wind_spread",
            "gust_spread",
            "precip_spread",
            "temp_spread",
            "hourly_risk",
        )
    )

    threshold = ThresholdProfile.objects.filter(site=site, is_active=True).first()
    thresholds = threshold.as_dict() if threshold else {
        "wind_mean_caution": 10.0, "wind_mean_cancel": 14.0,
        "gust_caution": 15.0, "gust_cancel": 20.0,
        "precip_caution": 0.7, "precip_cancel": 2.0,
        "temp_min_caution": 1.0, "temp_min_cancel": -2.0,
    }

    hourly_list = [
        {
            "time": h["timestamp"].isoformat(),
            "wind_speed": round(h["wind_speed"], 1),
            "wind_gusts": round(h["wind_gusts"], 1),
            "precipitation": round(h["precipitation"], 1),
            "temperature": round(h["temperature"], 1),
            "wind_spread": round(h["wind_spread"], 1),
            "gust_spread": round(h["gust_spread"], 1),
            "precip_spread": round(h["precip_spread"], 1),
            "temp_spread": round(h["temp_spread"], 1),
            "risk": round(h["hourly_risk"], 1),
        }
        for h in hourly_qs
    ]

    data = {
        "site": {
            "name": site.name,
            "postcode": site.postcode,
            "exposure": site.get_exposure_display(),
        },
        "thresholds": thresholds,
        "hourly": hourly_list,
        "debug": {
            "run_ids": run_ids,
            "hourly_count": len(hourly_list),
        },
    }

    return json.dumps(data)


@login_required(login_url="/login/")
def home(request):
    """
    Main dashboard view with live forecast data.
    """
    user = request.user
    sites_qs = _get_user_sites(user)
    sites_list = _annotate_sites_with_forecasts(sites_qs)

    total_sites = len(sites_list)
    sites_with_forecasts = sum(1 for s in sites_list if s.latest_run)
    alerts = sum(
        1
        for s in sites_list
        if s.latest_run and s.latest_run.recommendation in ("CAUTION", "CANCEL")
    )

    latest_ts = None
    for s in sites_list:
        if s.latest_run:
            if latest_ts is None or s.latest_run.generated_at > latest_ts:
                latest_ts = s.latest_run.generated_at

    context = {
        "user": user,
        "sites": sites_list,
        "site_count": total_sites,
        "forecast_count": sites_with_forecasts,
        "alert_count": alerts,
        "latest_forecast_time": latest_ts,
    }

    return render(request, "dashboard/home.html", context)


@login_required(login_url="/login/")
def site_detail(request, site_id):
    """
    Site detail view with full forecast display.
    Hourly data is embedded as inline JSON — no separate AJAX call needed.
    """
    user = request.user

    if user.is_superadmin:
        site = get_object_or_404(Site, pk=site_id, is_active=True)
    elif user.client:
        site = get_object_or_404(
            Site, pk=site_id, client=user.client, is_active=True
        )
    else:
        return render(request, "dashboard/no_access.html", status=403)

    today = date.today()
    runs = ForecastRun.objects.filter(
        site=site,
        status=ForecastRun.Status.SUCCESS,
        forecast_date__gte=today,
    ).order_by("forecast_date", "-generated_at")

    seen_dates = set()
    forecast_days = []
    for run in runs:
        if run.forecast_date not in seen_dates:
            seen_dates.add(run.forecast_date)
            forecast_days.append(run)

    from sites.models import ThresholdProfile
    threshold = ThresholdProfile.objects.filter(site=site, is_active=True).first()

    # Build hourly data as inline JSON — embedded in the page, no AJAX needed
    chart_data_json = _build_chart_data(site, forecast_days)

    context = {
        "user": user,
        "site": site,
        "forecast_days": forecast_days,
        "threshold": threshold,
        "today": today,
        "chart_data_json": chart_data_json,
    }

    return render(request, "dashboard/site_detail.html", context)


@login_required(login_url="/login/")
def forecast_chart_data(request, site_id):
    """
    JSON API endpoint — kept as a debug tool.
    Visit /dashboard/site/<id>/chart-data/ to inspect raw data.
    """
    user = request.user

    if user.is_superadmin:
        site = get_object_or_404(Site, pk=site_id, is_active=True)
    elif user.client:
        site = get_object_or_404(
            Site, pk=site_id, client=user.client, is_active=True
        )
    else:
        return JsonResponse({"error": "Access denied"}, status=403)

    today = date.today()

    runs = ForecastRun.objects.filter(
        site=site,
        status=ForecastRun.Status.SUCCESS,
        forecast_date__gte=today - timedelta(days=1),
    ).order_by("forecast_date", "-generated_at")

    seen = set()
    run_ids = []
    for run in runs:
        if run.forecast_date not in seen:
            seen.add(run.forecast_date)
            run_ids.append(run.pk)

    hourly = (
        HourlyForecast.objects.filter(run_id__in=run_ids)
        .order_by("timestamp")
        .values(
            "timestamp",
            "wind_speed",
            "wind_gusts",
            "precipitation",
            "temperature",
            "wind_spread",
            "gust_spread",
            "precip_spread",
            "temp_spread",
            "hourly_risk",
        )
    )

    from sites.models import ThresholdProfile
    threshold = ThresholdProfile.objects.filter(site=site, is_active=True).first()
    thresholds = threshold.as_dict() if threshold else {
        "wind_mean_caution": 10.0, "wind_mean_cancel": 14.0,
        "gust_caution": 15.0, "gust_cancel": 20.0,
        "precip_caution": 0.7, "precip_cancel": 2.0,
        "temp_min_caution": 1.0, "temp_min_cancel": -2.0,
    }

    hourly_list = list(hourly)

    data = {
        "site": {
            "name": site.name,
            "postcode": site.postcode,
            "exposure": site.get_exposure_display(),
        },
        "thresholds": thresholds,
        "debug": {
            "run_ids": run_ids,
            "hourly_count": len(hourly_list),
        },
        "hourly": [
            {
                "time": h["timestamp"].isoformat(),
                "wind_speed": round(h["wind_speed"], 1),
                "wind_gusts": round(h["wind_gusts"], 1),
                "precipitation": round(h["precipitation"], 1),
                "temperature": round(h["temperature"], 1),
                "wind_spread": round(h["wind_spread"], 1),
                "gust_spread": round(h["gust_spread"], 1),
                "precip_spread": round(h["precip_spread"], 1),
                "temp_spread": round(h["temp_spread"], 1),
                "risk": round(h["hourly_risk"], 1),
            }
            for h in hourly_list
        ],
    }

    return JsonResponse(data)
