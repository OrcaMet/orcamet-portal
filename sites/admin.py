from django.contrib import admin, messages
from .forms import SiteAdminForm
from .models import Client, Site, ThresholdProfile, ChangeLog
from .signals import queue_forecast_generation


class SiteInline(admin.TabularInline):
    model = Site
    form = SiteAdminForm
    extra = 0
    fields = ("name", "postcode", "latitude", "longitude", "exposure", "is_active", "job_complete")
    readonly_fields = ("latitude", "longitude")


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = (
        "name", "contact_name", "contact_email", "is_active",
        "is_sandbox", "self_service_sites", "site_count", "setup", "defaults",
    )
    list_filter = ("is_active", "is_sandbox", "self_service_sites")
    search_fields = ("name", "contact_name", "contact_email")
    inlines = [SiteInline]
    actions = ["reset_onboarding"]

    @admin.display(description="Active Sites")
    def site_count(self, obj):
        used = obj.site_set.filter(is_active=True).count()
        if not obj.self_service_sites:
            return used
        return f"{used} of {obj.effective_site_limit}"

    @admin.display(description="New-site defaults")
    def defaults(self, obj):
        """What a site added by this client will be created with."""
        from .presets import PRESETS

        preset = PRESETS[obj.effective_preset]["label"]
        return f"{preset} / {obj.effective_exposure_label}"

    @admin.display(description="Setup")
    def setup(self, obj):
        if not obj.self_service_sites:
            return "Staff-managed"
        if obj.onboarding_completed_at:
            return f"Done {obj.onboarding_completed_at:%d %b %Y}"
        return "Not started"

    @admin.action(description="Re-run onboarding for selected clients")
    def reset_onboarding(self, request, queryset):
        """
        Send these clients back through the setup wizard on next login.

        Creates and changes nothing else — their sites, users and thresholds
        are untouched. Useful when a client wants to revisit their limits
        with someone walking them through it.
        """
        updated = queryset.update(onboarding_completed_at=None)
        self.message_user(
            request,
            f"{updated} client(s) will see the setup wizard next time an "
            f"admin of theirs logs in.",
            messages.SUCCESS,
        )


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    # Geocodes during validation, so a bad postcode is a form error rather
    # than a silently coordinate-less site. Site.save() no longer does it.
    form = SiteAdminForm

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
