"""
OrcaMet Portal — generate_contour_cache management command

Builds a UK-wide risk contour map: fetches a coarse ensemble grid,
computes peak work-hour risk per point, renders a contour plot, and
caches it as a UKRiskMap record for today.

Usage:
    python manage.py generate_contour_cache
"""

import base64
import io
import logging
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from django.conf import settings
from django.core.management.base import BaseCommand

from forecasts.engine.uk_grid import build_grid, fetch_grid_ensemble, compute_grid_risk
from forecasts.models import UKRiskMap

logger = logging.getLogger(__name__)

WORK_START = getattr(settings, "FORECAST_WORK_START_HOUR", 7)
WORK_END = getattr(settings, "FORECAST_WORK_END_HOUR", 18)


class Command(BaseCommand):
    help = "Generate the UK-wide risk contour map and cache it as a UKRiskMap record"

    def handle(self, *args, **options):
        today = datetime.now(timezone.utc).date()

        points = build_grid()
        self.stdout.write(f"Fetching ensemble for {len(points)} grid points across the UK...")

        blended = fetch_grid_ensemble(
            points,
            start_date=today.strftime("%Y-%m-%d"),
            end_date=today.strftime("%Y-%m-%d"),
        )
        if not blended:
            self.stderr.write("No grid data returned — aborting")
            return

        risk_by_point = compute_grid_risk(blended, WORK_START, WORK_END)
        if not risk_by_point:
            self.stderr.write("No risk scores computed — aborting")
            return

        lats = np.array([p[0] for p in risk_by_point])
        lons = np.array([p[1] for p in risk_by_point])
        risks = np.array([risk_by_point[p] for p in risk_by_point])

        peak_idx = int(np.argmax(risks))

        image_b64 = self._render_contour(lats, lons, risks)

        UKRiskMap.objects.filter(forecast_date=today).delete()
        UKRiskMap.objects.create(
            forecast_date=today,
            image_data=image_b64,
            peak_risk=float(risks[peak_idx]),
            peak_location_lat=float(lats[peak_idx]),
            peak_location_lon=float(lons[peak_idx]),
            grid_points=len(risk_by_point),
        )

        self.stdout.write(self.style.SUCCESS(
            f"UK risk map cached for {today}: {len(risk_by_point)} points, "
            f"peak risk {risks[peak_idx]:.1f}% at ({lats[peak_idx]:.2f}, {lons[peak_idx]:.2f})"
        ))

    def _render_contour(self, lats, lons, risks) -> str:
        # Matches the portal's ocean-blue theme (orcamet_portal/static/orcamet_portal/css/portal.css)
        bg = "#1e293b"     # --surface
        fg = "#f1f5f9"     # --text-primary
        grid = "#334155"   # --border

        fig, ax = plt.subplots(figsize=(7, 9))
        fig.patch.set_facecolor(bg)
        ax.set_facecolor(bg)

        contour = ax.tricontourf(
            lons, lats, risks,
            levels=np.linspace(0, 100, 11),
            cmap="RdYlGn_r",
        )
        ax.set_xlabel("Longitude", color=fg)
        ax.set_ylabel("Latitude", color=fg)
        ax.set_title("OrcaMet UK Risk Map — Peak Work-Hour Risk (%)", color=fg)
        ax.tick_params(colors=fg)
        for spine in ax.spines.values():
            spine.set_color(grid)
        ax.set_aspect(1.6)  # rough correction for longitude compression at UK latitudes

        cbar = fig.colorbar(contour, ax=ax, label="Risk %")
        cbar.ax.yaxis.label.set_color(fg)
        cbar.ax.tick_params(colors=fg)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor=bg)
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("ascii")
