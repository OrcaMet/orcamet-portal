"""
OrcaMet Portal — Onboarding forms.
"""

from django import forms

from sites.models import Client, Site, ThresholdProfile
from sites.presets import DEFAULT_PRESET, preset_choices


class OperationForm(forms.ModelForm):
    """
    Step 2: who you are, and what limits you work to.

    Both defaults are saved onto the Client, not parked in the session.
    They outlive onboarding: a site added six months later has to get the
    same limits and the same exposure as the ones imported on day one, and
    only the Client row is visible to the code paths that create it.
    """

    class Meta:
        model = Client
        fields = [
            "contact_name", "contact_email", "contact_phone",
            "threshold_preset", "default_exposure",
        ]
        labels = {
            "contact_name": "Main contact",
            "contact_email": "Email",
            "contact_phone": "Phone",
            "threshold_preset": "Starting limits",
            "default_exposure": "Typical setting",
        }
        widgets = {
            "contact_name": forms.TextInput(attrs={"autofocus": True}),
            "contact_phone": forms.TextInput(attrs={"placeholder": "Optional"}),
            # Both are plain CharFields on the model — Client is declared
            # before Site, so Exposure's choices are not available there.
            # The choices belong here, where they are asked for.
            "threshold_preset": forms.RadioSelect(),
            "default_exposure": forms.Select(),
        }
        help_texts = {
            "contact_email": "Where we send anything that needs your attention.",
            "threshold_preset": (
                "We will apply these to every site you add next. You can "
                "change them on the following step, and per site at any time."
            ),
            "default_exposure": (
                "Used for sites you import without saying otherwise, and for "
                "new sites you add later. It tells the forecast how exposed "
                "the location is."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Required in the form even though both are blank=True on the model:
        # blank is what a client predating onboarding has, and the whole
        # point of this step is to stop guessing on their behalf.
        self.fields["threshold_preset"].choices = preset_choices()
        self.fields["threshold_preset"].required = True
        self.fields["default_exposure"].choices = Site.Exposure.choices
        self.fields["default_exposure"].required = True

        # A client who has not answered yet gets the sensible default
        # pre-selected rather than an empty radio group.
        if not self.instance.threshold_preset:
            self.initial.setdefault("threshold_preset", DEFAULT_PRESET)
        if not self.instance.default_exposure:
            self.initial.setdefault("default_exposure", Site.Exposure.URBAN)


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
