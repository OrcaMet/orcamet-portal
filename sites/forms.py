"""
OrcaMet Portal — Site forms.

SiteForm backs the self-service (sandbox) views; SiteAdminForm backs the
Django admin. Both geocode here rather than in Site.save(), so the network
call happens where a failure can be reported to whoever typed the postcode.
"""

from django import forms

from .models import Site, geocode_postcode


class GeocodedPostcodeMixin:
    """
    Resolve the postcode to coordinates during validation.

    Site.save() no longer geocodes, so without this a site would be stored
    with NULL coordinates and never get a forecast.
    """

    _geocoded = None

    def clean_postcode(self):
        postcode = (self.cleaned_data.get("postcode") or "").strip().upper()
        if not postcode:
            return postcode

        # Unchanged postcode on an edit: the coordinates are already good, so
        # don't spend a postcodes.io call re-confirming them.
        if self.instance.pk and (self.instance.postcode or "").strip().upper() == postcode:
            return postcode

        lat, lon = geocode_postcode(postcode)
        if lat is None:
            raise forms.ValidationError(
                "We couldn't find that UK postcode. Please check it and try again."
            )

        self._geocoded = (lat, lon)
        return postcode

    def _post_clean(self):
        """
        Apply the geocoded coordinates to the instance.

        This has to happen here, not in clean_postcode: _post_clean rebuilds
        the instance from cleaned_data, so anything written to the instance
        during field cleaning is overwritten whenever latitude/longitude are
        themselves form fields — silently storing a site with no coordinates.
        """
        super()._post_clean()
        if self._geocoded is not None:
            self.instance.latitude, self.instance.longitude = self._geocoded


class SiteAdminForm(GeocodedPostcodeMixin, forms.ModelForm):
    """Admin form for Site, with the same postcode validation as the portal."""

    class Meta:
        model = Site
        fields = "__all__"


class SiteForm(GeocodedPostcodeMixin, forms.ModelForm):
    """
    Create/edit form for a site owned by the logged-in user's client.

    `client` is deliberately absent: the view sets it from request.user, so a
    tester cannot post a client id and attach a site to somebody else's
    workspace.
    """

    class Meta:
        model = Site
        fields = ["name", "postcode", "exposure", "elevation", "notes"]
        widgets = {
            "name": forms.TextInput(attrs={
                "placeholder": "e.g. Tower Block A",
                "autofocus": True,
            }),
            "postcode": forms.TextInput(attrs={"placeholder": "e.g. EH1 1YZ"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "elevation": "Metres above sea level. Leave at 0 if unsure.",
            "notes": "Optional.",
        }

    def __init__(self, *args, client=None, **kwargs):
        self.client = client
        super().__init__(*args, **kwargs)

    def clean_name(self):
        """
        Enforce Site's unique_together ourselves.

        `client` is excluded from the form, so Django's own uniqueness check
        cannot run and a duplicate name would surface as an IntegrityError.
        """
        name = (self.cleaned_data.get("name") or "").strip()
        if not name or self.client is None:
            return name

        clash = Site.objects.filter(client=self.client, name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("You already have a site with that name.")
        return name
