"""
OrcaMet Portal — Onboarding forms.
"""

from django import forms

from sites.models import Client, Site, ThresholdProfile
from sites.presets import DEFAULT_PRESET, preset_choices


class OperationForm(forms.ModelForm):
    """
    Step 2: who you are, and what limits you work to.

    The preset is not a Client field — it seeds the ThresholdProfile of every
    site created in step 3 and is then adjustable per site, so storing it on
    the client would imply a permanence it does not have. It rides in the
    session between steps instead.
    """

    threshold_preset = forms.ChoiceField(
        choices=preset_choices,
        initial=DEFAULT_PRESET,
        widget=forms.RadioSelect,
        label="Starting limits",
        help_text=(
            "We will apply these to every site you add next. You can change "
            "them on the following step, and per site at any time."
        ),
    )
    default_exposure = forms.ChoiceField(
        choices=Site.Exposure.choices,
        initial=Site.Exposure.URBAN,
        label="Typical setting",
        help_text=(
            "Used for sites you import without saying otherwise. It tells the "
            "forecast how exposed the location is."
        ),
    )

    class Meta:
        model = Client
        fields = ["contact_name", "contact_email", "contact_phone"]
        labels = {
            "contact_name": "Main contact",
            "contact_email": "Email",
            "contact_phone": "Phone",
        }
        widgets = {
            "contact_name": forms.TextInput(attrs={"autofocus": True}),
            "contact_phone": forms.TextInput(attrs={"placeholder": "Optional"}),
        }
        help_texts = {
            "contact_email": "Where we send anything that needs your attention.",
        }


class ThresholdsForm(forms.ModelForm):
    """
    Step 4: review the limits that were applied, and change them if wrong.

    A ModelForm over ThresholdProfile so its `clean()` — which rejects
    orderings the risk engine cannot interpret, such as a cancel below a
    caution — applies here exactly as it does in the admin.
    """

    apply_to_all = forms.BooleanField(
        required=False,
        initial=True,
        label="Apply to all of my sites",
        help_text=(
            "Leave ticked while you are setting up. Untick to change only "
            "the site shown."
        ),
    )

    class Meta:
        model = ThresholdProfile
        fields = [
            "wind_mean_caution", "wind_mean_cancel",
            "gust_caution", "gust_cancel",
            "precip_caution", "precip_cancel",
            "temp_min_caution", "temp_min_cancel",
            "temp_max_caution", "temp_max_cancel",
        ]
        # The model's own verbose names read as field names ("Temp min
        # caution") and carry no unit, which is the one thing somebody
        # typing a number into the box needs to know.
        labels = {
            "wind_mean_caution": "Wind — caution (m/s)",
            "wind_mean_cancel": "Wind — stop (m/s)",
            "gust_caution": "Gusts — caution (m/s)",
            "gust_cancel": "Gusts — stop (m/s)",
            "precip_caution": "Rain — caution (mm/h)",
            "precip_cancel": "Rain — stop (mm/h)",
            "temp_min_caution": "Cold — caution (°C)",
            "temp_min_cancel": "Cold — stop (°C)",
            "temp_max_caution": "Heat — caution (°C)",
            "temp_max_cancel": "Heat — stop (°C)",
        }
