from django.contrib import admin, messages
from .models import Client, Site, ThresholdProfile, ChangeLog
from .signals import queue_forecast_generation


class SiteInline(admin.TabularInline):
    model = Site
    extra = 0
    fields = ("name", "postcode", "latitude", "longitude", "exposure", "is_active", "job_complete")
    readonly_fields = ("latitude", "longitude")


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_name", "contact_email", "is_active", "is_sandbox", "site_count")
    list_filter = ("is_active", "is_sandbox")
    search_fields = ("name", "contact_name", "contact_email")
    inlines = [SiteInline]

    def site_count(self, obj):
        return obj.site_set.filter(is_active=True).count()
    site_count.short_description = "Active Sites"


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
        started = 0
        already_running = 0
        skipped = 0

        for site in queryset:
            # `is None` rather than falsiness — longitude 0.0 is a valid UK
            # location (the Greenwich meridian runs through Cambridgeshire).
            if site.latitude is None or site.longitude is None or not site.is_active:
                skipped += 1
                continue
            if queue_forecast_generation(site.pk, site.name):
                started += 1
            else:
                already_running += 1

        parts = [f"Forecast generation started for {started} site(s)."]
        if already_running:
            parts.append(f"{already_running} already had a run in progress.")
        if skipped:
            parts.append(f"{skipped} skipped (inactive or no coordinates).")

        messages.success(request, " ".join(parts))


@admin.register(ThresholdProfile)
class ThresholdProfileAdmin(admin.ModelAdmin):
    list_display = (
        "site", "is_active",
        "wind_mean_cancel", "gust_cancel", "precip_cancel",
        "temp_min_cancel", "temp_max_cancel",
        "created_at", "created_by",
    )
    list_filter = ("is_active", "site__client")

    fieldsets = (
        (None, {"fields": ("site", "is_active")}),
        ("Wind (mean, 10m) — m/s", {"fields": ("wind_mean_caution", "wind_mean_cancel")}),
        ("Gusts — m/s", {"fields": ("gust_caution", "gust_cancel")}),
        ("Precipitation — mm/h", {"fields": ("precip_caution", "precip_cancel")}),
        ("Temperature — cold (°C)", {
            "fields": ("temp_min_caution", "temp_min_cancel"),
            "description": "Colder is worse, so cancel must be <em>lower</em> than caution.",
        }),
        ("Temperature — heat (°C)", {
            "fields": ("temp_max_caution", "temp_max_cancel"),
            "description": (
                "Hotter is worse, so cancel must be <em>higher</em> than caution. "
                "Leave both blank to ignore heat at this site. Cold and heat share "
                "one temperature weight — whichever end is worse at a given hour "
                "counts, so temperature never scores twice."
            ),
        }),
        ("Audit", {"fields": ("created_by",)}),
    )


@admin.register(ChangeLog)
class ChangeLogAdmin(admin.ModelAdmin):
    list_display = ("site", "action", "user", "timestamp")
    list_filter = ("action", "site__client")
    readonly_fields = ("site", "action", "details", "user", "timestamp")
