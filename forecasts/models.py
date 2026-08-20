"""
OrcaMet Portal — Forecast Models

ForecastRun: A single execution of the forecast engine for a site.
HourlyForecast: Individual hourly data points within a run.
UKRiskGridRun: A single execution of the UK-wide multi-model risk grid.
UKRiskGridPoint: Hourly ensemble weather/risk data at one grid point.
CachedContourImage: Pre-rendered per-variable, per-hour contour PNGs.
MapThresholds: Editable thresholds behind the UK map's Risk layer.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
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


class MapThresholds(models.Model):
    """
    The thresholds behind the UK map's Risk layer, editable in the admin.

    A single row (singleton) — the map is UK-wide, so unlike a site it has no
    per-location profile to draw on. These were previously a hardcoded dict in
    the risk_grid command, which meant a code change and a deploy to adjust.

    Only the Risk layer uses these. The wind, gust, precipitation and
    temperature layers show raw forecast values on fixed colour scales.

    Changes do not alter the map until the grid is rebuilt: the map serves
    pre-rendered contour PNGs, regenerated by the risk_grid cron every 6
    hours.
    """

    # Kept in sync with ThresholdProfile's field defaults, so an untouched
    # site scores the same as the map underneath it.
    wind_mean_caution = models.FloatField(default=10.0, help_text="Wind caution threshold (m/s)")
    wind_mean_cancel = models.FloatField(default=14.0, help_text="Wind cancel threshold (m/s)")

    gust_caution = models.FloatField(default=15.0, help_text="Gust caution threshold (m/s)")
    gust_cancel = models.FloatField(default=20.0, help_text="Gust cancel threshold (m/s)")

    precip_caution = models.FloatField(default=0.7, help_text="Precipitation caution (mm/h)")
    precip_cancel = models.FloatField(default=2.0, help_text="Precipitation cancel (mm/h)")

    temp_min_caution = models.FloatField(default=1.0, help_text="Cold caution threshold (°C)")
    temp_min_cancel = models.FloatField(default=-2.0, help_text="Cold cancel threshold (°C)")

    # Nullable so heat scoring can be switched off entirely; blank means
    # cold-only, exactly as the map behaved before heat existed.
    temp_max_caution = models.FloatField(
        default=27.0, null=True, blank=True,
        help_text="Heat caution threshold (°C). Leave blank to ignore heat.",
    )
    temp_max_cancel = models.FloatField(
        default=32.0, null=True, blank=True,
        help_text="Heat cancel threshold (°C). Leave blank to ignore heat.",
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        editable=False,
    )

    class Meta:
        verbose_name = "UK map risk thresholds"
        verbose_name_plural = "UK map risk thresholds"

    def __str__(self):
        return "UK map risk thresholds"

    # The fields the forecast engine expects, in the order they are validated.
    FIELDS = (
        "wind_mean_caution", "wind_mean_cancel",
        "gust_caution", "gust_cancel",
        "precip_caution", "precip_cancel",
        "temp_min_caution", "temp_min_cancel",
        "temp_max_caution", "temp_max_cancel",
    )

    def save(self, *args, **kwargs):
        # Force the singleton: any save writes to the one row.
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Never delete — risk_grid depends on a row being here."""
        raise ValidationError("The UK map thresholds row cannot be deleted.")

    @classmethod
    def load(cls):
        """Return the singleton, creating it with the defaults if absent."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def clean(self):
        """
        Reject orderings the risk engine cannot interpret.

        core.ramp() ramps from the caution value to the cancel value. For
        wind, gust and precipitation, higher is worse, so cancel must sit
        above caution; for temperature, lower is worse, so cancel must sit
        below. Inverting either silently produces a flat ramp — the layer
        would keep rendering, just wrong, with nothing to indicate why.
        """
        errors = {}

        for name in ("wind_mean", "gust", "precip"):
            caution = getattr(self, f"{name}_caution")
            cancel = getattr(self, f"{name}_cancel")
            if caution is None or cancel is None:
                continue
            if cancel <= caution:
                errors[f"{name}_cancel"] = (
                    "Cancel must be higher than caution — higher values are worse "
                    f"for this variable (caution is {caution})."
                )

        if self.temp_min_caution is not None and self.temp_min_cancel is not None:
            if self.temp_min_cancel >= self.temp_min_caution:
                errors["temp_min_cancel"] = (
                    "Cold cancel must be lower than cold caution — colder is worse "
                    f"(caution is {self.temp_min_caution})."
                )

        # Both heat fields or neither: temperature_ramp() ignores a lone value,
        # so a half-filled pair would silently do nothing.
        if (self.temp_max_caution is None) != (self.temp_max_cancel is None):
            errors["temp_max_cancel"] = (
                "Set both heat thresholds, or leave both blank to ignore heat."
            )
        elif self.temp_max_caution is not None:
            if self.temp_max_cancel <= self.temp_max_caution:
                errors["temp_max_cancel"] = (
                    "Heat cancel must be higher than heat caution — hotter is worse "
                    f"(caution is {self.temp_max_caution})."
                )
            if (self.temp_min_caution is not None
                    and self.temp_max_caution <= self.temp_min_caution):
                errors["temp_max_caution"] = (
                    "Heat caution must be warmer than cold caution "
                    f"({self.temp_min_caution}°C), or the two ends overlap."
                )

        if errors:
            raise ValidationError(errors)

    def as_dict(self):
        """Thresholds in the shape calculate_hourly_risk() expects."""
        return {name: getattr(self, name) for name in self.FIELDS}
