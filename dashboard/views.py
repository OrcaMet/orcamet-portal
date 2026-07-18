"""
OrcaMet Portal — Dashboard Views

The main views a logged-in user sees.
"""
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render, get_object_or_404

from forecasts.models import ForecastRun, UKRiskMap
from sites.models import Site


@login_required(login_url="/login/")
def home(request):
    """
    Main dashboard view.

    Superadmins see all sites.
    Client users see only their client's sites.
    """
    user = request.user

    if user.is_superadmin:
        sites_list = Site.objects.filter(is_active=True).select_related("client")
    elif user.client:
        sites_list = Site.objects.filter(
            client=user.client, is_active=True
        ).select_related("client")
    else:
        sites_list = Site.objects.none()

    sites_list = list(sites_list)

    # Latest forecast run per site — attached directly onto each site object
    # so the template can read site.latest_run without extra queries.
    for site in sites_list:
        site.latest_run = (
            ForecastRun.objects.filter(site=site).order_by("-generated_at").first()
        )

    generated_times = [s.latest_run.generated_at for s in sites_list if s.latest_run]
    alert_count = sum(
        1 for s in sites_list
        if s.latest_run
        and s.latest_run.status == ForecastRun.Status.SUCCESS
        and s.latest_run.recommendation in ("CAUTION", "CANCEL")
    )

    context = {
        "user": user,
        "sites": sites_list,
        "site_count": len(sites_list),
        "latest_forecast_at": max(generated_times) if generated_times else None,
        "alert_count": alert_count,
        "uk_risk_map": UKRiskMap.objects.order_by("-generated_at").first(),
    }

    return render(request, "dashboard/home.html", context)

@login_required(login_url="/login/")
def site_detail(request, site_id):
    """
    Detail view for a single site.

    Shows the latest forecast run, hourly breakdown, active thresholds,
    and recent forecast history for one site.
    """
    user = request.user

    if user.is_superadmin:
        site = get_object_or_404(Site, pk=site_id, is_active=True)
    elif user.client:
        site = get_object_or_404(Site, pk=site_id, client=user.client, is_active=True)
    else:
        raise Http404("Site not found")

    latest_run = ForecastRun.objects.filter(site=site).order_by("-generated_at").first()
    hourly = latest_run.hourly_data.all() if latest_run else []
    thresholds = site.thresholds.filter(is_active=True).first()
    past_runs = ForecastRun.objects.filter(site=site).order_by("-generated_at")[1:8]

    context = {
        "user": user,
        "site": site,
        "run": latest_run,
        "hourly": hourly,
        "thresholds": thresholds,
        "past_runs": past_runs,
    }

    return render(request, "dashboard/site_detail.html", context)
