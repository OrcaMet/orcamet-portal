"""
OrcaMet Portal — Forecast Views

Views for the UK-wide risk map detail page.
"""

import base64

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .models import CachedContourImage, UKRiskGridRun, UKRiskMap
@login_required(login_url="/login/")
def risk_map_detail(request):
    """
    Detailed view of the latest UK-wide risk map, including the
    underlying grid run metadata and any cached contour images.
    """
    uk_risk_map = UKRiskMap.objects.order_by("-generated_at").first()
    grid_run = UKRiskGridRun.objects.order_by("-generated_at").first()

    contour_layers = []
    if grid_run:
        latest_per_variable = {}
        images = CachedContourImage.objects.filter(run=grid_run).order_by("-timestamp")
        for image in images:
            if image.variable not in latest_per_variable:
                latest_per_variable[image.variable] = image

        for image in latest_per_variable.values():
            contour_layers.append({
                "label": image.get_variable_display(),
                "timestamp": image.timestamp,
                "b64": base64.b64encode(bytes(image.image_data)).decode("ascii"),
            })

    context = {
        "user": request.user,
        "uk_risk_map": uk_risk_map,
        "grid_run": grid_run,
        "contour_layers": contour_layers,
    }

    return render(request, "forecasts/risk_map_detail.html", context)
