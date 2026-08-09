"""
OrcaMet Portal — Forecast Views

Views for the UK-wide risk map detail page.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


@login_required(login_url="/login/")
def risk_map_detail(request):
    """
    Legacy UK risk map route.

    This view used to render "forecasts/risk_map_detail.html", but that
    template does not exist anywhere in the project, so every request raised
    TemplateDoesNotExist and returned a 500 in production. The page was
    superseded by the interactive map at dashboard:weather_map (the dashboard
    link was repointed there), so redirect rather than 500 — old bookmarks
    keep working.
    """
    return redirect("dashboard:weather_map")
