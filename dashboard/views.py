"""
OrcaMet Portal — Dashboard Views

The main views a logged-in user sees.
"""

import json
import math
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.http import (
    Http404, HttpResponse, HttpResponseNotModified, JsonResponse,
)
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from forecasts.models import ForecastRun, HourlyForecast, UKRiskGridRun, UKRiskGridPoint, CachedContourImage, MapThresholds
from sites.models import Site, ThresholdProfile
from dashboard.map_colours import colour_ramps
from dashboard.map_legend import legend_data


def _visible_sites(user):
    """Return the queryset of sites this user is allowed to see."""
    if user.is_superadmin:
        return Site.objects.filter(is_active=True).select_related("client")
    elif user.client:
        return Site.objects.filter(client=user.client, is_active=True).select_related("client")
    return Site.objects.none()


def _num(value):
    """
    Return a JSON-safe number, or None for anything non-finite.

    json.dumps emits bare NaN/Infinity tokens, which are not valid JSON. One
    non-finite value used to make an entire map response unparseable in the
    browser, which silently emptied the map.
    """
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# Threshold keys as they should read to a human.
_CAUSE_LABELS = {
    "gust_cancel": "gusts",
    "wind_mean_cancel": "wind",
    "precip_cancel": "rain",
    "temperature": "temperature",
}


def _annotate_cancellation(run):
    """
    Attach display-ready cancellation figures to a ForecastRun.

    Kept out of the template because a probability needs rounding once, in
    one place — and because a null must never be rendered as 0%, which would
    read as "certainly fine" rather than "we do not know".
    """
    if run.p_cancel is None:
        run.p_cancel_pct = None
        run.p_cancel_causes = ""
        return

    run.p_cancel_pct = round(run.p_cancel * 100)

    # Biggest contributor first — the reason someone would act.
    causes = sorted(
        (run.p_cancel_by_variable or {}).items(),
        key=lambda kv: -kv[1],
    )
    run.p_cancel_causes = ", ".join(
        f"{_CAUSE_LABELS.get(key, key)} {round(share * 100)}%"
        for key, share in causes
        if share > 0
    )


# ============================================================
# FRAME CACHING
# ============================================================
#
# A map frame addressed by run key, variable and hour can never change: a
# grid run's rows are written once and a new run gets a new key. Serving
# those with no caching headers at all meant every frame of a 72-hour
# playback was a fresh database round trip pulling a BLOB through a worker,
# repeated for every viewer and again every time the variable tabs were
# switched.
#
# Frames reached by falling back — no run key, or an hour with no exact
# match — are not immutable, because "the latest run" changes under the
# client. Those get a short window instead.
#
# `private` rather than `public`: the payload is UK-wide and not specific to
# the user, but it is served from a login-gated URL, and a shared proxy
# holding responses to authenticated requests is not a trade worth making
# for a cache that only needs to be per-browser.
IMMUTABLE_MAX_AGE = 60 * 60 * 24 * 30   # 30 days — outlives any run
FALLBACK_MAX_AGE = 60                    # 1 minute


def _frame_etag(prefix, ident, variable, timestamp):
    """A stable identifier for one immutable frame."""
    stamp = int(timestamp.timestamp()) if timestamp else 0
    return f'"{prefix}{ident}-{variable}-{stamp}"'


def _cache_frame(response, etag, immutable):
    """Attach validation and freshness headers to a frame response."""
    response["ETag"] = etag
    if immutable:
        response["Cache-Control"] = (
            f"private, max-age={IMMUTABLE_MAX_AGE}, immutable"
        )
    else:
        response["Cache-Control"] = f"private, max-age={FALLBACK_MAX_AGE}"
    return response


# CARTO serves these tiles unauthenticated, but stamps every one with
# "API KEY REQUIRED" until a key is supplied.
CARTO_BASEMAP_TEMPLATE = (
    "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
)


def basemap_url():
    """
    The basemap tile URL, with the CARTO key appended when one is configured.

    Falls back to the unauthenticated URL rather than breaking the map, so a
    missing key costs a watermark and nothing else.
    """
    key = getattr(settings, "CARTO_API_KEY", "")
    if not key:
        return CARTO_BASEMAP_TEMPLATE
    # CARTO's parameter is `key`. Verified empirically against the live CDN:
    # with `key` the tile comes back clean, while api_key/apikey/token — and
    # no parameter at all — return a byte-identical watermarked tile.
    return f"{CARTO_BASEMAP_TEMPLATE}?key={quote(key, safe='')}"


def _latest_runs_by_site(sites, success_only=True):
    """
    Return {site_id: most recent ForecastRun} for the given sites.

    Uses a single query. Callers previously looped over sites issuing one
    query each, so a client with N sites cost N queries per page load.
    """
    runs = ForecastRun.objects.filter(site__in=sites)
    if success_only:
        runs = runs.filter(status=ForecastRun.Status.SUCCESS)

    latest = {}
    # Newest run for each site comes first, so the first one wins.
    for run in runs.order_by("site_id", "-generated_at"):
        latest.setdefault(run.site_id, run)
    return latest


def _timeline_runs(sites, days=None):
    """
    Latest successful run per (site, forecast_date) from today onward.

    The map timeline spans the grid's full horizon (72 hours by default), but
    a single ForecastRun only covers one day. Using just the newest run meant
    the hourly frames ran out after 24 hours, and the markers silently fell
    back to a peak-of-day summary for the rest of the timeline — the contour
    kept advancing hour by hour while the pins showed a different metric,
    with nothing on screen saying so.
    """
    if days is None:
        days = getattr(settings, "FORECAST_NUM_DAYS", 3)

    today = timezone.localdate()

    latest_ids = (
        ForecastRun.objects
        .filter(
            site__in=sites,
            status=ForecastRun.Status.SUCCESS,
            forecast_date__gte=today,
            forecast_date__lt=today + timedelta(days=days),
        )
        .values("site_id", "forecast_date")
        .annotate(latest_id=Max("id"))
        .values_list("latest_id", flat=True)
    )

    return list(ForecastRun.objects.filter(id__in=latest_ids).order_by("forecast_date"))


@login_required(login_url="/login/")
def home(request):
    """
    Main dashboard view.

    Superadmins see all sites.
    Client users see only their client's sites.
    """
    user = request.user
    sites_list = list(_visible_sites(user))

    # Latest run of any status, matching the previous behaviour.
    latest_runs = _latest_runs_by_site(sites_list, success_only=False)
    for site in sites_list:
        site.latest_run = latest_runs.get(site.id)

    generated_times = [s.latest_run.generated_at for s in sites_list if s.latest_run]
    alert_count = sum(1 for s in sites_list if s.latest_run and s.latest_run.status == ForecastRun.Status.SUCCESS and s.latest_run.recommendation in ("CAUTION", "CANCEL"))

    context = {"user": user, "sites": sites_list, "site_count": len(sites_list), "latest_forecast_at": max(generated_times) if generated_times else None, "alert_count": alert_count}

    # Trial accounts manage their own sites, within a cap.
    if user.is_sandbox_user:
        context["sandbox_site_cap"] = settings.SANDBOX_MAX_SITES
        context["sandbox_may_add"] = len(sites_list) < settings.SANDBOX_MAX_SITES

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

    for run in forecast_days:
        _annotate_cancellation(run)

    threshold = site.thresholds.filter(is_active=True).first()

    if threshold:
        thresholds_dict = threshold.as_dict()
    else:
        thresholds_dict = {"wind_mean_caution": 10.0, "wind_mean_cancel": 14.0, "gust_caution": 15.0, "gust_cancel": 20.0, "precip_caution": 0.7, "precip_cancel": 2.0, "temp_min_caution": 1.0, "temp_min_cancel": -2.0, "temp_max_caution": 27.0, "temp_max_cancel": 32.0}

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

    sites_list = list(sites_qs)
    latest_runs = _latest_runs_by_site(sites_list)

    recommendations = [
        run.recommendation
        for run in (latest_runs.get(site.id) for site in sites_list)
        if run and run.recommendation
    ]

    latest_grid_run = UKRiskGridRun.objects.filter(status=UKRiskGridRun.Status.SUCCESS).order_by("-generated_at").first()

    data_age_hours = None
    last_grid_update = None
    if latest_grid_run:
        last_grid_update = latest_grid_run.generated_at
        delta = timezone.now() - last_grid_update
        data_age_hours = round(delta.total_seconds() / 3600, 1)

    context = {"user": user, "data_age_hours": data_age_hours, "last_grid_update": last_grid_update, "go_count": recommendations.count("GO"), "caution_count": recommendations.count("CAUTION"), "cancel_count": recommendations.count("CANCEL")}

    # The map scores the hourly site markers client-side, because the hourly
    # values come from a JSON frame rather than a stored ForecastRun. It used
    # to do that against thresholds hardcoded in the template, so editing them
    # in the admin moved the contour layer but left the markers scoring
    # against the old numbers — and heat never applied there at all.
    context["map_thresholds"] = MapThresholds.load().as_dict()
    context["basemap_url"] = basemap_url()

    # Legend colours come from the same colormaps the contours are rendered
    # with. The template used to carry its own table, which had drifted: the
    # wind key showed green at 7 m/s where YlOrRd is pale yellow.
    context["map_legend"] = legend_data()

    # The colour ramps the browser paints the field with. Same colormaps the
    # server renderer used, so a colour keeps meaning what it always meant.
    context["map_colours"] = colour_ramps()

    return render(request, "dashboard/weather_map.html", context)


@login_required(login_url="/login/")
def map_sites_json(request):
    """GeoJSON of all visible sites with their latest forecast summary."""
    user = request.user
    sites_qs = _visible_sites(user)

    sites_list = list(sites_qs)
    latest_runs = _latest_runs_by_site(sites_list)

    features = []
    for site in sites_list:
        if site.coords is None:
            continue

        run = latest_runs.get(site.id)

        props = {
            "id": site.id,
            "name": site.name,
            "client": site.client.name,
            "postcode": site.postcode,
            "exposure": site.get_exposure_display(),
            "has_forecast": bool(run),
            "recommendation": run.recommendation if run else None,
            # The default contour layer is a chance of cancellation, and the
            # pin beside it showed only a severity score — two different
            # measures with nothing saying so. This is the same quantity the
            # contour draws, computed for this site's own thresholds.
            # None stays None: a null must never render as 0%, which reads as
            # "certainly fine" rather than "we do not know".
            "p_cancel": (
                round(run.p_cancel * 100) if run and run.p_cancel is not None
                else None
            ),
            "limiting_variable": run.limiting_variable if run else None,
            "peak_risk": _num(run.peak_risk) if run else None,
            "peak_wind": _num(run.peak_wind) if run else None,
            "peak_gust": _num(run.peak_gust) if run else None,
            "peak_precip": _num(run.peak_precip) if run else None,
            "min_temp": _num(run.min_temp) if run else None,
            "forecast_date": run.forecast_date.isoformat() if run else None,
        }
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [site.longitude, site.latitude],
            },
            "properties": props,
        })

    return JsonResponse({"type": "FeatureCollection", "features": features})


@login_required(login_url="/login/")
def map_sites_hourly_json(request):
    """Per-timestamp GeoJSON frames of hourly data for all visible sites."""
    user = request.user
    sites_qs = _visible_sites(user)

    sites_list = [s for s in sites_qs if s.coords is not None]

    # Every upcoming day's run, so the frames cover the same horizon as the
    # contour timeline rather than stopping after the first day.
    runs = _timeline_runs(sites_list)
    sites_by_id = {s.id: s for s in sites_list}

    # One query for every run's hourly data instead of one per site.
    hours_by_run = {run.id: [] for run in runs}
    if runs:
        for hour in HourlyForecast.objects.filter(
            run_id__in=hours_by_run
        ).order_by("timestamp"):
            hours_by_run[hour.run_id].append(hour)

    # A site can have several runs (one per day); merge them into one series.
    hours_by_site = {}
    for run in runs:
        site = sites_by_id.get(run.site_id)
        if site is None:
            continue
        hours_by_site.setdefault(site.id, []).extend(hours_by_run.get(run.id, []))

    # Each site's own limits, so a pin is scored the way that site is scored
    # everywhere else in the portal. The map used to gate every marker against
    # the single UK-wide MapThresholds row, which gave a sheltered site and an
    # exposed one identical verdicts on identical weather — the exposure
    # column in the popup said they differed while the colour said they did
    # not. Sent once per site rather than per feature: repeating ten numbers
    # across every site in every one of 72 frames is most of the payload.
    site_thresholds = {}
    for profile in ThresholdProfile.objects.filter(
        site__in=sites_list, is_active=True
    ):
        # One active profile per site; first wins if the data says otherwise.
        site_thresholds.setdefault(str(profile.site_id), profile.as_dict())

    site_hours = []
    timestamps_set = set()

    for site_id, hours in hours_by_site.items():
        if not hours:
            continue
        hours.sort(key=lambda h: h.timestamp)
        site_hours.append((sites_by_id[site_id], hours))
        for h in hours:
            timestamps_set.add(h.timestamp)

    timestamps = sorted(timestamps_set)
    ts_strs = [t.isoformat() for t in timestamps]
    frames = {ts: {"features": []} for ts in ts_strs}

    for site, hours in site_hours:
        for h in hours:
            ts_str = h.timestamp.isoformat()
            frames[ts_str]["features"].append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [site.longitude, site.latitude],
                },
                "properties": {
                    "id": site.id,
                    "name": site.name,
                    "client": site.client.name,
                    "postcode": site.postcode,
                    "wind_speed": _num(h.wind_speed),
                    "wind_gusts": _num(h.wind_gusts),
                    "precipitation": _num(h.precipitation),
                    "temperature": _num(h.temperature),
                },
            })

    return JsonResponse({
        "timestamps": ts_strs,
        "frames": frames,
        "thresholds": site_thresholds,
    })


@login_required(login_url="/login/")
def map_contour_image(request):
    """Serve a single cached contour PNG for a variable/timestamp."""
    var_name = request.GET.get("var", "risk")
    timestamp = request.GET.get("timestamp")
    run_key = request.GET.get("run")

    if var_name not in CachedContourImage.Variable.values:
        raise Http404("Unknown contour variable")

    # The client sends the run key it built its timeline from. Honour it, so
    # that a grid run completing mid-session cannot cause frames from the new
    # run to be served against the old run's timestamps.
    # The client falls back to a timestamp string when no run key is known,
    # so only treat a well-formed integer as a primary key.
    run = None
    if run_key and run_key.isdigit():
        run = UKRiskGridRun.objects.filter(
            pk=int(run_key), status=UKRiskGridRun.Status.SUCCESS
        ).first()

    pinned_run = run is not None

    if run is None:
        run = UKRiskGridRun.objects.filter(
            status=UKRiskGridRun.Status.SUCCESS
        ).order_by("-generated_at").first()

    if not run:
        raise Http404("No grid run available")

    images = CachedContourImage.objects.filter(run=run, variable=var_name)

    # Identify the frame before fetching it. The PNG is a BLOB on the row,
    # so selecting the whole row to decide whether the client already has it
    # would pull the very bytes a 304 exists to avoid sending.
    row = None
    exact = False

    if timestamp:
        parsed = parse_datetime(timestamp)
        if parsed:
            row = images.filter(timestamp=parsed).values_list("id", "timestamp").first()
            exact = row is not None

    if row is None:
        row = images.order_by("timestamp").values_list("id", "timestamp").first()

    if row is None:
        raise Http404("No contour image available")

    image_id, image_ts = row
    etag = _frame_etag("c", image_id, var_name, image_ts)

    # Immutable only when the client named both the run and the hour and got
    # exactly them. A frame reached by falling back to "the latest run" or
    # "the first hour" is a moving target and must not be cached for long.
    immutable = pinned_run and exact

    if request.headers.get("If-None-Match") == etag:
        return _cache_frame(HttpResponseNotModified(), etag, immutable)

    data = (
        CachedContourImage.objects
        .filter(pk=image_id)
        .values_list("image_data", flat=True)
        .first()
    )
    if data is None:
        raise Http404("No contour image available")

    return _cache_frame(
        HttpResponse(bytes(data), content_type="image/png"), etag, immutable
    )


@login_required(login_url="/login/")
def map_grid_points_json(request):
    """
    Raw grid point values for a run/timestamp, powering the map's
    hover-to-read-value tooltip. The contour image is a static PNG with
    no client-side access to the underlying numbers, so this exposes
    them separately for a nearest-point lookup under the cursor.
    """
    run_key = request.GET.get("run")
    timestamp = request.GET.get("timestamp")

    run = None
    if run_key and run_key.isdigit():
        run = UKRiskGridRun.objects.filter(
            pk=int(run_key), status=UKRiskGridRun.Status.SUCCESS
        ).first()

    pinned_run = run is not None

    if run is None:
        run = UKRiskGridRun.objects.filter(
            status=UKRiskGridRun.Status.SUCCESS
        ).order_by("-generated_at").first()

    if not run:
        return JsonResponse({"points": []})

    points_qs = UKRiskGridPoint.objects.filter(run=run)

    parsed = parse_datetime(timestamp) if timestamp else None
    if parsed:
        points_qs = points_qs.filter(timestamp=parsed)
        frame_ts = parsed
    else:
        first_ts = points_qs.order_by("timestamp").values_list("timestamp", flat=True).first()
        points_qs = points_qs.filter(timestamp=first_ts) if first_ts else points_qs.none()
        frame_ts = first_ts

    # Same reasoning as the contour frames: a run's points are written once,
    # so a fully addressed frame can be revalidated instead of rebuilt.
    etag = _frame_etag("g", run.pk, "points", frame_ts)
    immutable = pinned_run and parsed is not None

    if request.headers.get("If-None-Match") == etag:
        return _cache_frame(HttpResponseNotModified(), etag, immutable)

    # Flat arrays instead of objects — this list is a few hundred entries
    # fetched once per frame, so the key-name overhead of a dict per point
    # adds up.
    points = [
        # Index 7 is p_cancel; the client's FIELD_FOR_VAR maps by position.
        # Indexes 8 and 9 are wind direction and how much the members agreed
        # on it — appended, never inserted, because that positional mapping
        # is a contract with the template.
        [p.latitude, p.longitude, _num(p.risk), _num(p.wind_speed), _num(p.wind_gusts), _num(p.precipitation), _num(p.temperature), _num(p.p_cancel), _num(p.wind_direction), _num(p.wind_direction_agreement)]
        for p in points_qs
    ]

    return _cache_frame(JsonResponse({"points": points}), etag, immutable)


@login_required(login_url="/login/")
def map_contour_timestamps(request):
    """List available contour timestamps for the latest UK risk grid run."""
    run = UKRiskGridRun.objects.filter(status=UKRiskGridRun.Status.SUCCESS).order_by("-generated_at").first()

    if not run:
        response = JsonResponse({"available": False, "timestamps": [], "has_cache": False})
        response["Cache-Control"] = "no-store"
        return response

    timestamps = list(UKRiskGridPoint.objects.filter(run=run).values_list("timestamp", flat=True).distinct().order_by("timestamp"))
    has_cache = CachedContourImage.objects.filter(run=run).exists()

    response = JsonResponse({"available": True, "timestamps": [t.isoformat() for t in timestamps], "has_cache": has_cache, "models_used": run.models_used, "grid_points": run.grid_points, "run_key": str(run.id), "generated_at": run.generated_at.isoformat()})

    # Deliberately not cached like the frames are. This is the discovery
    # document that hands the client its run key, and it is what tells a
    # long-open map that a new grid run exists. Caching it would pin the
    # session to a stale run and defeat the frame keys entirely.
    response["Cache-Control"] = "no-store"
    return response
