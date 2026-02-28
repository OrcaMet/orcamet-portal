"""
OrcaMet Portal — Sites Models

Client: A rope access company using OrcaMet's services.
Site: A specific work location with postcode, lat/lon, and thresholds.
ThresholdProfile: Configurable weather limits for a site.
ChangeLog: Audit trail for threshold and site changes.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class Client(models.Model):
    """A rope access company that subscribes to OrcaMet forecasts."""

    name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=200, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def active_sites(self):
        return self.site_set.filter(is_active=True)


class Site(models.Model):
    """A specific work location for a client."""

    class Exposure(models.TextChoices):
        URBAN = "urban", "Urban"
        COASTAL = "coastal", "Coastal"
        HIGHLAND = "highland", "Highland"
        RURAL = "rural", "Rural"

    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    postcode = models.CharField(
        max_length=10,
        help_text="UK postcode — will be automatically geocoded to lat/lon",
    )
    latitude = models.FloatField(
        null=True, blank=True,
        help_text="Auto-populated from postcode via postcodes.io",
    )
    longitude = models.FloatField(
        null=True, blank=True,
        help_text="Auto-populated from postcode via postcodes.io",
    )
    elevation = models.IntegerField(
        default=0,
        help_text="Elevation in metres above sea level",
    )
    exposure = models.CharField(
        max_length=20,
        choices=Exposure.choices,
        default=Exposure.URBAN,
    )
    is_active = models.BooleanField(default=True)
    job_complete = models.BooleanField(
        default=False,
        help_text="When True, forecast generation stops for this site",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["client", "name"]
        unique_together = ["client", "name"]

    def __str__(self):
        return f"{self.name} ({self.client.name})"

    @property
    def coords(self):
        if self.latitude and self.longitude:
            return (self.latitude, self.longitude)
        return None

    def save(self, *args, **kwargs):
        """Auto-geocode postcode on save if lat/lon not set."""
        if self.postcode and not (self.latitude and self.longitude):
            lat, lon = geocode_postcode(self.postcode)
            if lat is not None:
                self.latitude = lat
                self.longitude = lon
        super().save(*args, **kwargs)


class ThresholdProfile(models.Model):
    """
    Weather thresholds for a site.

    Each site has one active threshold profile.
    When thresholds change, the old profile is deactivated
    and a new one created (with a ChangeLog entry).
    """

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="thresholds")
    is_active = models.BooleanField(default=True)

    # Wind thresholds (m/s)
    wind_mean_caution = models.FloatField(default=10.0, help_text="Wind caution threshold (m/s)")
    wind_mean_cancel = models.FloatField(default=14.0, help_text="Wind cancel threshold (m/s)")

    # Gust thresholds (m/s)
    gust_caution = models.FloatField(default=15.0, help_text="Gust caution threshold (m/s)")
    gust_cancel = models.FloatField(default=20.0, help_text="Gust cancel threshold (m/s)")

    # Precipitation thresholds (mm/h)
    precip_caution = models.FloatField(default=0.7, help_text="Precipitation caution (mm/h)")
    precip_cancel = models.FloatField(default=2.0, help_text="Precipitation cancel (mm/h)")

    # Temperature thresholds (°C)
    temp_min_caution = models.FloatField(default=1.0, help_text="Temperature caution (°C)")
    temp_min_cancel = models.FloatField(default=-2.0, help_text="Temperature cancel (°C)")

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Thresholds for {self.site.name} ({'Active' if self.is_active else 'Archived'})"

    def as_dict(self):
        """Return thresholds as a dictionary for the forecast engine."""
        return {
            "wind_mean_caution": self.wind_mean_caution,
            "wind_mean_cancel": self.wind_mean_cancel,
            "gust_caution": self.gust_caution,
            "gust_cancel": self.gust_cancel,
            "precip_caution": self.precip_caution,
            "precip_cancel": self.precip_cancel,
            "temp_min_caution": self.temp_min_caution,
            "temp_min_cancel": self.temp_min_cancel,
        }


class ChangeLog(models.Model):
    """Audit trail for site and threshold changes."""

    class Action(models.TextChoices):
        SITE_CREATED = "site_created", "Site Created"
        SITE_UPDATED = "site_updated", "Site Updated"
        SITE_DEACTIVATED = "site_deactivated", "Site Deactivated"
        THRESHOLD_CREATED = "threshold_created", "Threshold Created"
        THRESHOLD_UPDATED = "threshold_updated", "Threshold Updated"

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="changelog")
    action = models.CharField(max_length=30, choices=Action.choices)
    details = models.JSONField(
        default=dict,
        help_text="JSON object describing what changed",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.get_action_display()} — {self.site.name} — {self.timestamp:%Y-%m-%d %H:%M}"


# ============================================================
# POSTCODE GEOCODING
# ============================================================

def geocode_postcode(postcode: str) -> tuple:
    """
    Convert a UK postcode to lat/lon using postcodes.io (free, no key needed).

    Returns (latitude, longitude) or (None, None) on failure.
    """
    import requests

    clean = postcode.strip().upper()
    url = f"https://api.postcodes.io/postcodes/{clean}"

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") == 200 and data.get("result"):
            result = data["result"]
            return (result["latitude"], result["longitude"])
    except Exception:
        pass

    return (None, None)
