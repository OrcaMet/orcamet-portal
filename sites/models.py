"""
OrcaMet Portal — Sites Models

Client: A rope access company using OrcaMet's services.
Site: A specific work location with postcode, lat/lon, and thresholds.
ThresholdProfile: Configurable weather limits for a site.
ChangeLog: Audit trail for threshold and site changes.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Client(models.Model):
    """A rope access company that subscribes to OrcaMet forecasts."""

    name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=200, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)
    is_sandbox = models.BooleanField(
        default=False,
        help_text=(
            "A private trial workspace created by an invite link, not a real "
            "paying client. Sandbox owners may add and edit their own sites "
            "through the portal, subject to SANDBOX_MAX_SITES."
        ),
    )
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
        # Compare against None rather than testing truthiness: longitude 0.0
        # is a valid UK coordinate (the Greenwich meridian runs through
        # London, Cambridge and East Anglia) and would otherwise be treated
        # as "no coordinates", silently hiding the site.
        if self.latitude is not None and self.longitude is not None:
            return (self.latitude, self.longitude)
        return None

    def geocode_if_needed(self) -> bool:
        """
        Fill in lat/lon from the postcode. Returns False if the lookup failed.

        Deliberately not called from save(). It used to be, which put a
        10-second-timeout HTTP call to postcodes.io inside every Site write —
        holding a request or admin thread for the duration — and, on failure,
        silently stored NULL coordinates, producing a site that never gets a
        forecast with nothing to explain why. Callers that accept user input
        geocode through their form instead, where a failure becomes a
        validation error the user can act on.
        """
        if not self.postcode:
            return False
        if self.latitude is not None and self.longitude is not None:
            return True

        lat, lon = geocode_postcode(self.postcode)
        if lat is None:
            return False

        self.latitude = lat
        self.longitude = lon
        return True


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

    # Temperature thresholds (°C) — cold end
    temp_min_caution = models.FloatField(default=1.0, help_text="Cold caution threshold (°C)")
    temp_min_cancel = models.FloatField(default=-2.0, help_text="Cold cancel threshold (°C)")

    # Temperature thresholds (°C) — heat end.
    # Nullable so heat scoring can be switched off for a site where it does
    # not apply; blank means cold-only, exactly as before heat existed.
    temp_max_caution = models.FloatField(
        default=27.0, null=True, blank=True,
        help_text="Heat caution threshold (°C). Leave blank to ignore heat for this site.",
    )
    temp_max_cancel = models.FloatField(
        default=32.0, null=True, blank=True,
        help_text="Heat cancel threshold (°C). Leave blank to ignore heat for this site.",
    )

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
            # None here means "no heat scoring" — temperature_ramp() treats a
            # missing pair as cold-only.
            "temp_max_caution": self.temp_max_caution,
            "temp_max_cancel": self.temp_max_cancel,
        }

    def clean(self):
        """
        Reject orderings the risk engine cannot interpret.

        ramp() runs from the caution value to the cancel value, so cold cancel
        must sit below cold caution, and heat cancel above heat caution.
        Inverting either produces a flat ramp that keeps scoring, just wrong.
        """
        errors = {}

        for name in ("wind_mean", "gust", "precip"):
            caution = getattr(self, f"{name}_caution")
            cancel = getattr(self, f"{name}_cancel")
            if caution is not None and cancel is not None and cancel <= caution:
                errors[f"{name}_cancel"] = (
                    f"Cancel must be higher than caution (caution is {caution})."
                )

        if self.temp_min_caution is not None and self.temp_min_cancel is not None:
            if self.temp_min_cancel >= self.temp_min_caution:
                errors["temp_min_cancel"] = (
                    "Cold cancel must be lower than cold caution "
                    f"(caution is {self.temp_min_caution})."
                )

        # Both heat fields or neither: one alone is ignored by the engine, so
        # a half-filled pair silently does nothing.
        if (self.temp_max_caution is None) != (self.temp_max_cancel is None):
            errors["temp_max_cancel"] = (
                "Set both heat thresholds, or leave both blank to ignore heat."
            )
        elif self.temp_max_caution is not None:
            if self.temp_max_cancel <= self.temp_max_caution:
                errors["temp_max_cancel"] = (
                    "Heat cancel must be higher than heat caution "
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
