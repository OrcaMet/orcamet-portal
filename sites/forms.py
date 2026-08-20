"""
OrcaMet Portal — Site forms for the self-service (sandbox) views.
"""

from django import forms

from .models import Site, geocode_postcode


class SiteForm(forms.ModelForm):
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

    def clean_postcode(self):
        """
        Reject a postcode we cannot geocode.

        Site.save() geocodes silently and leaves lat/lon as None on failure,
        which produces a site that never gets a forecast and gives the user no
        clue why. Better to fail here, where we can say so.
        """
        postcode = (self.cleaned_data.get("postcode") or "").strip().upper()
        if not postcode:
            return postcode

        # Unchanged postcode on an edit: the coordinates are already good, so
        # don't spend a postcodes.io call re-confirming them.
        if self.instance.pk and self.instance.postcode.strip().upper() == postcode:
            return postcode

        lat, lon = geocode_postcode(postcode)
        if lat is None:
            raise forms.ValidationError(
                "We couldn't find that UK postcode. Please check it and try again."
            )

        # Stash the result so save() doesn't repeat the lookup.
        self.instance.latitude = lat
        self.instance.longitude = lon
        return postcode

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
