from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import ForecastLock, ForecastRun, HourlyForecast, MapThresholds


class HourlyForecastInline(admin.TabularInline):
    model = HourlyForecast
    extra = 0
    readonly_fields = (
        "timestamp", "wind_speed", "wind_gusts", "precipitation",
        "temperature", "hourly_risk",
    )


@admin.register(ForecastRun)
class ForecastRunAdmin(admin.ModelAdmin):
    list_display = (
        "site", "forecast_date", "status", "peak_risk",
        "recommendation", "generated_at",
    )
    list_filter = ("status", "site__client", "forecast_date")
    inlines = [HourlyForecastInline]


@admin.register(ForecastLock)
class ForecastLockAdmin(admin.ModelAdmin):
    """
    Visibility into in-progress forecast runs.

    Locks clear themselves — on completion, or after locking.STALE_AFTER if
    the worker died mid-run. This exists so a stuck site can be freed
    immediately rather than waiting out that window, and so "why is this site
    not updating" has somewhere to look.
    """

    list_display = ("site", "acquired_at", "holder")
    readonly_fields = ("site", "acquired_at", "holder")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MapThresholds)
class MapThresholdsAdmin(admin.ModelAdmin):
    """
    Singleton editor for the UK map's risk thresholds.

    Presented as one always-there settings page rather than a list: there is
    exactly one row, adding a second is meaningless, and deleting it would
    break the risk_grid run.
    """

    readonly_fields = ("updated_at", "updated_by")

    fieldsets = (
        ("Wind (mean, 10m)", {
            "fields": ("wind_mean_caution", "wind_mean_cancel"),
            "description": "Metres per second. Contributes 30% of the risk score.",
        }),
        ("Gusts", {
            "fields": ("gust_caution", "gust_cancel"),
            "description": "Metres per second. The largest single contributor, at 40%.",
        }),
        ("Precipitation", {
            "fields": ("precip_caution", "precip_cancel"),
            "description": "Millimetres per hour. Contributes 20%.",
        }),
        ("Temperature — cold", {
            "fields": ("temp_min_caution", "temp_min_cancel"),
            "description": (
                "Degrees Celsius. Colder is worse, so cancel must be "
                "<em>lower</em> than caution."
            ),
        }),
        ("Temperature — heat", {
            "fields": ("temp_max_caution", "temp_max_cancel"),
            "description": (
                "Degrees Celsius. Hotter is worse, so cancel must be "
                "<em>higher</em> than caution. Leave both blank to ignore heat "
                "entirely. Cold and heat share one 10% temperature weight — "
                "whichever end is worse at a given hour is the one that counts, "
                "so temperature never scores twice."
            ),
        }),
        ("Last change", {
            "fields": ("updated_at", "updated_by"),
        }),
    )

    def has_add_permission(self, request):
        # The row is created on demand by MapThresholds.load().
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        """Skip the one-row list and go straight to the settings form."""
        obj = MapThresholds.load()
        return HttpResponseRedirect(
            reverse("admin:forecasts_mapthresholds_change", args=[obj.pk])
        )

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

        # The map serves pre-rendered contour PNGs, so nothing visible changes
        # until the grid is rebuilt. Saying so here avoids the obvious
        # conclusion that the setting simply did not work.
        messages.warning(
            request,
            "Saved. The map still shows the previous thresholds until the grid "
            "is rebuilt — this happens automatically on the next risk_grid run "
            "(every 6 hours), or immediately if you run "
            "'python manage.py risk_grid' from the Render shell.",
        )
