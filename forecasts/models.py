"""
OrcaMet Portal — Forecast Models

ForecastRun: A single execution of the forecast engine for a site.
HourlyForecast: Individual hourly data points within a run.
UKRiskGridRun: A single execution of the UK-wide multi-model risk grid.
UKRiskGridPoint: Hourly ensemble weather/risk data at one grid point.
CachedContourImage: Pre-rendered per-variable, per-hour contour PNGs.
"""

from django.db import models
from django.utils import timezone


class ForecastRun(models.Model):
    """
    A single forecast generation run for a specific site.
    Created twice daily by the cron job.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    site = models.ForeignKey(
        "sites.Site",
        on_delete=models.CASCADE,
        related_name="forecast_runs",
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    forecast_date = models.DateField(
        help_text="The date this forecast is valid for",
    )
    generated_at = models.DateTimeField(default=timezone.now)

    # Summary statistics for quick display
    peak_risk = models.FloatField(null=True, blank=True, help_text="Peak risk % for the day")
    recommendation = models.CharField(max_length=20, blank=True, help_text="GO / CAUTION / CANCEL")
    peak_wind = models.FloatField(null=True, blank=True, help_text="Peak wind speed (m/s)")
    peak_gust = models.FloatField(null=True, blank=True, help_text="Peak gust (m/s)")
    peak_precip = models.FloatField(null=True, blank=True, help_text="Peak precipitation (mm/h)")
    min_temp = models.FloatField(null=True, blank=True, help_text="Minimum temperature (°C)")

    # Models used in ensemble
    models_used = models.JSONField(default=list, help_text="List of weather model names used")

    # Optional: stored chart image as base64 or file path
    chart_image = models.TextField(blank=True, help_text="Base64-encoded forecast chart PNG")
    text_report = models.TextField(blank=True, help_text="Generated text report")

    # Threshold snapshot (so we know what thresholds were active when forecast was generated)
    thresholds_snapshot = models.JSONField(
        default=dict,
        help_text="Copy of threshold values used for this forecast run",
    )

    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-generated_at"]
        # One forecast per site per date per run
        unique_together = ["site", "forecast_date", "generated_at"]

    def __str__(self):
        return f"{self.site.name} — {self.forecast_date} — {self.get_status_display()}"


class HourlyForecast(models.Model):
    """Individual hourly forecast data point within a run."""

    run = models.ForeignKey(
        ForecastRun,
        on_delete=models.CASCADE,
        related_name="hourly_data",
    )
    timestamp = models.DateTimeField()

    # Ensemble-blended values
    wind_speed = models.FloatField(help_text="Ensemble mean wind speed (m/s)")
    wind_gusts = models.FloatField(help_text="Ensemble mean gusts (m/s)")
    precipitation = models.FloatField(help_text="Ensemble mean precipitation (mm/h)")
    temperature = models.FloatField(help_text="Ensemble mean temperature (°C)")

    # Model spread (uncertainty)
    wind_spread = models.FloatField(default=0.0)
    gust_spread = models.FloatField(default=0.0)
    precip_spread = models.FloatField(default=0.0)
    temp_spread = models.FloatField(default=0.0)

    # Computed risk
    hourly_risk = models.FloatField(help_text="Risk score 0-100%")

    class Meta:
        ordering = ["timestamp"]

    def __str__(self):
        return f"{self.run.site.name} — {self.timestamp:%H:%M} — Risk: {self.hourly_risk:.0f}%"


class UKRiskGridRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    forecast_date = models.DateField()
    generated_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    lat_min = models.FloatField(default=49.9)
    lat_max = models.FloatField(default=58.7)
    lon_min = models.FloatField(default=-7.6)
    lon_max = models.FloatField(default=1.8)
    resolution = models.FloatField(default=0.5, help_text="Grid spacing in degrees")
    grid_points = models.IntegerField(default=0)
    num_hours = models.IntegerField(default=0)
    models_used = models.JSONField(default=list)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-generated_at"]

    def __str__(self):
        return f"UK Risk Grid Run — {self.forecast_date} — {self.get_status_display()}"

class UKRiskGridPoint(models.Model):
    run = models.ForeignKey(UKRiskGridRun, on_delete=models.CASCADE, related_name="points")
    latitude = models.FloatField()
    longitude = models.FloatField()
    timestamp = models.DateTimeField()
    wind_speed = models.FloatField(default=0.0)
    wind_gusts = models.FloatField(default=0.0)
    precipitation = models.FloatField(default=0.0)
    temperature = models.FloatField(default=0.0)
    risk = models.FloatField(default=0.0, help_text="Risk score 0-100%")

    class Meta:
        ordering = ["timestamp", "latitude", "longitude"]
        indexes = [models.Index(fields=["run", "timestamp"], name="forecasts_u_run_id_3b3b76_idx")]

    def __str__(self):
        return f"({self.latitude:.2f}, {self.longitude:.2f}) — {self.timestamp:%Y-%m-%d %H:%M} — {self.risk:.0f}%"

class CachedContourImage(models.Model):
    class Variable(models.TextChoices):
        RISK = "risk", "Risk"
        WIND = "wind", "Wind Speed"
        GUST = "gust", "Wind Gusts"
        PRECIP = "precip", "Precipitation"
        TEMP = "temp", "Temperature"

    run = models.ForeignKey(UKRiskGridRun, on_delete=models.CASCADE, related_name="cached_images")
    timestamp = models.DateTimeField()
    variable = models.CharField(max_length=10, choices=Variable.choices)
    image_data = models.BinaryField(help_text="PNG image bytes (pre-rendered contour map)")

    class Meta:
        ordering = ["variable", "timestamp"]
        indexes = [models.Index(fields=["run", "variable", "timestamp"], name="forecasts_c_run_id_37aedb_idx")]
        unique_together = [("run", "timestamp", "variable")]

    def __str__(self):
        return f"{self.get_variable_display()} contour — {self.timestamp:%Y-%m-%d %H:%M}"
