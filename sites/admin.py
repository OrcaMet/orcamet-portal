import threading
import logging

from django.contrib import admin, messages
from .models import Client, Site, ThresholdProfile, ChangeLog

logger = logging.getLogger(__name__)


class SiteInline(admin.TabularInline):
    model = Site
    extra = 0
    fields = ("name", "postcode", "latitude", "longitude", "exposure", "is_active", "job_complete")
    readonly_fields = ("latitude", "longitude")


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_name", "contact_email", "is_active", "site_count")
    list_filter = ("is_active",)
    search_fields = ("name", "contact_name", "contact_email")
    inlines = [SiteInline]

    def site_count(self, obj):
        return obj.site_set.filter(is_active=True).count()
    site_count.short_description = "Active Sites"


def _run_forecast_bg(site_id):
    """Background thread for manual forecast trigger."""
    try:
        from forecasts.engine.runner import run_forecast_for_site
        site = Site.objects.get(pk=site_id)
        run_forecast_for_site(site)
    except Exception as e:
        logger.error(f"Manual forecast failed for site {site_id}: {e}", exc_info=True)


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = (
        "name", "client", "postcode", "latitude", "longitude",
        "exposure", "is_active", "job_complete", "latest_risk",
    )
    list_filter = ("client", "exposure", "is_active", "job_complete")
    search_fields = ("name", "postcode")
    readonly_fields = ("latitude", "longitude", "created_at")
    actions = ["generate_forecasts"]

    def latest_risk(self, obj):
        """Show the latest peak risk in the list view."""
        from forecasts.models import ForecastRun
        run = ForecastRun.objects.filter(
            site=obj, status="success"
        ).order_by("-forecast_date").first()
        if run and run.peak_risk is not None:
            emoji = {"GO": "🟢", "CAUTION": "🟡", "CANCEL": "🔴"}.get(run.recommendation, "⚪")
            return f"{emoji} {run.peak_risk:.0f}% {run.recommendation}"
        return "—"
    latest_risk.short_description = "Latest Risk"

    @admin.action(description="Generate forecasts for selected sites")
    def generate_forecasts(self, request, queryset):
        count = 0
        for site in queryset:
            if site.latitude and site.longitude and site.is_active:
                thread = threading.Thread(
                    target=_run_forecast_bg,
                    args=(site.pk,),
                    daemon=True,
                )
                thread.start()
                count += 1

        messages.success(
            request,
            f"Forecast generation started for {count} site(s). "
            f"Refresh in ~30 seconds to see results."
        )


@admin.register(ThresholdProfile)
class ThresholdProfileAdmin(admin.ModelAdmin):
    list_display = (
        "site", "is_active",
        "wind_mean_cancel", "gust_cancel", "precip_cancel", "temp_min_cancel",
        "created_at", "created_by",
    )
    list_filter = ("is_active", "site__client")


@admin.register(ChangeLog)
class ChangeLogAdmin(admin.ModelAdmin):
    list_display = ("site", "action", "user", "timestamp")
    list_filter = ("action", "site__client")
    readonly_fields = ("site", "action", "details", "user", "timestamp")
