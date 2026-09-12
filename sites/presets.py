"""
OrcaMet Portal — Threshold presets.

A new client has to pick weather limits before their first forecast means
anything, and "wind_mean_caution" with a number box is not a question most
operations managers can answer cold. These are the three starting points
offered during onboarding, each a complete set of ThresholdProfile values.

A preset is a starting point, not a recommendation to work to. The client
adjusts it in the last onboarding step and per site afterwards; the safe
answer for any given job is the one in their own method statement.

Values are in the model's units: wind and gust m/s, precipitation mm/h,
temperature °C. Multiply m/s by 1.944 for knots.
"""

from django.core.exceptions import ImproperlyConfigured

from .models import ThresholdProfile

# Keys of ThresholdProfile that a preset sets. Anything outside this set is
# rejected by _check_presets below, so a typo cannot silently do nothing.
PRESET_FIELDS = frozenset(ThresholdProfile().as_dict())


PRESETS = {
    "rope_access": {
        "label": "Rope access — standard",
        "summary": (
            "For general rope access and working at height. Caution around "
            "17 knots mean wind, stop by 27 knots, on the basis that a "
            "suspended worker is being moved by the gusts well before the "
            "mean wind is the problem."
        ),
        "thresholds": {
            "wind_mean_caution": 9.0,
            "wind_mean_cancel": 14.0,
            "gust_caution": 13.0,
            "gust_cancel": 18.0,
            "precip_caution": 0.5,
            "precip_cancel": 2.0,
            "temp_min_caution": 2.0,
            "temp_min_cancel": -2.0,
            "temp_max_caution": 27.0,
            "temp_max_cancel": 32.0,
        },
    },
    "crane_lifting": {
        "label": "Crane and lifting",
        "summary": (
            "Tighter wind limits for lifting, where a load presents a large "
            "sail area and the gust matters more than the mean. Rain and "
            "temperature are held wider, since they rarely stop a lift on "
            "their own."
        ),
        "thresholds": {
            "wind_mean_caution": 7.0,
            "wind_mean_cancel": 10.0,
            "gust_caution": 9.0,
            "gust_cancel": 12.5,
            "precip_caution": 1.0,
            "precip_cancel": 4.0,
            "temp_min_caution": 0.0,
            "temp_min_cancel": -5.0,
            "temp_max_caution": 30.0,
            "temp_max_cancel": 35.0,
        },
    },
    "conservative": {
        "label": "Conservative",
        "summary": (
            "Flags earlier across the board. Worth starting here if you are "
            "new to the portal and would rather see more cautions than miss "
            "one — you can loosen it once you have watched a few weeks "
            "against what your teams actually did."
        ),
        "thresholds": {
            "wind_mean_caution": 7.0,
            "wind_mean_cancel": 11.0,
            "gust_caution": 10.0,
            "gust_cancel": 15.0,
            "precip_caution": 0.3,
            "precip_cancel": 1.0,
            "temp_min_caution": 4.0,
            "temp_min_cancel": 0.0,
            "temp_max_caution": 25.0,
            "temp_max_cancel": 30.0,
        },
    },
}

DEFAULT_PRESET = "rope_access"


def preset_choices():
    """(key, label) pairs for a form field."""
    return [(key, preset["label"]) for key, preset in PRESETS.items()]


def thresholds_for(key):
    """
    The threshold values for a preset key, falling back to the default.

    Returns a fresh dict each call — the caller passes it straight into
    ThresholdProfile(**values), and a shared dict would be one accidental
    mutation away from changing everyone's limits.
    """
    preset = PRESETS.get(key) or PRESETS[DEFAULT_PRESET]
    return dict(preset["thresholds"])


def _check_presets():
    """
    Fail at import if a preset does not match ThresholdProfile.

    These dicts are splatted into the model constructor, so a renamed or
    dropped field would otherwise surface as a TypeError during a client's
    onboarding, or — for a missing key — as a site silently scored against
    model defaults instead of the preset.
    """
    for key, preset in PRESETS.items():
        keys = set(preset["thresholds"])
        if keys != PRESET_FIELDS:
            missing = sorted(PRESET_FIELDS - keys)
            unknown = sorted(keys - PRESET_FIELDS)
            raise ImproperlyConfigured(
                f"Threshold preset '{key}' does not match ThresholdProfile: "
                f"missing={missing}, unknown={unknown}"
            )

    if DEFAULT_PRESET not in PRESETS:
        raise ImproperlyConfigured(
            f"DEFAULT_PRESET '{DEFAULT_PRESET}' is not one of the presets."
        )


_check_presets()
