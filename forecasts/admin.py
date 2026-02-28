from django.contrib import admin
from .models import ForecastRun, HourlyForecast, UKRiskMap


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


@admin.register(UKRiskMap)
class UKRiskMapAdmin(admin.ModelAdmin):
    list_display = ("forecast_date", "peak_risk", "grid_points", "generated_at")
