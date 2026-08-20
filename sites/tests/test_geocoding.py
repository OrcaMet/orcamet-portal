"""
Tests for postcode geocoding after it moved out of Site.save().

save() used to make a 10-second-timeout HTTP call to postcodes.io on every
write, holding a request or admin thread, and silently stored NULL
coordinates when the lookup failed — producing a site that never gets a
forecast with nothing on screen to explain why.
"""

from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.test import TestCase

from sites.admin import SiteAdmin
from sites.forms import SiteAdminForm, SiteForm
from sites.models import Client, Site


@patch("sites.signals.queue_forecast_generation", return_value=True)
class SiteSaveTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Acme Rope")

    def test_save_makes_no_network_call(self, _queue):
        with patch("sites.models.geocode_postcode") as geo:
            Site.objects.create(
                client=self.client_obj, name="Tower", postcode="EH1 1YZ",
            )
        geo.assert_not_called()

    def test_geocode_if_needed_fills_coordinates(self, _queue):
        site = Site(client=self.client_obj, name="Tower", postcode="EH1 1YZ")

        with patch("sites.models.geocode_postcode", return_value=(55.95, -3.19)):
            self.assertTrue(site.geocode_if_needed())

        self.assertEqual(site.latitude, 55.95)

    def test_geocode_if_needed_reports_failure(self, _queue):
        site = Site(client=self.client_obj, name="Tower", postcode="ZZ99 9ZZ")

        with patch("sites.models.geocode_postcode", return_value=(None, None)):
            self.assertFalse(site.geocode_if_needed())

        self.assertIsNone(site.latitude)

    def test_geocode_if_needed_skips_when_already_located(self, _queue):
        site = Site(
            client=self.client_obj, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )

        with patch("sites.models.geocode_postcode") as geo:
            self.assertTrue(site.geocode_if_needed())

        geo.assert_not_called()

    def test_greenwich_longitude_is_not_treated_as_missing(self, _queue):
        """longitude 0.0 is a real UK coordinate, not an absent one."""
        site = Site(
            client=self.client_obj, name="Meridian", postcode="CB1 1AA",
            latitude=52.2, longitude=0.0,
        )

        with patch("sites.models.geocode_postcode") as geo:
            site.geocode_if_needed()

        geo.assert_not_called()


@patch("sites.signals.queue_forecast_generation", return_value=True)
class AdminFormGeocodingTests(TestCase):
    """The admin must get the same validation the portal form gives."""

    def setUp(self):
        self.client_obj = Client.objects.create(name="Acme Rope")

    def _data(self, **overrides):
        data = {
            "client": self.client_obj.pk,
            "name": "Tower",
            "postcode": "EH1 1YZ",
            "elevation": 0,
            "exposure": "urban",
            "is_active": "on",
            "notes": "",
        }
        data.update(overrides)
        return data

    def test_valid_postcode_populates_coordinates(self, _queue):
        with patch("sites.forms.geocode_postcode", return_value=(55.95, -3.19)):
            form = SiteAdminForm(self._data())
            self.assertTrue(form.is_valid(), form.errors)
            site = form.save()

        self.assertEqual(site.latitude, 55.95)
        self.assertEqual(site.longitude, -3.19)

    def test_ungeocodable_postcode_is_a_form_error(self, _queue):
        with patch("sites.forms.geocode_postcode", return_value=(None, None)):
            form = SiteAdminForm(self._data(postcode="ZZ99 9ZZ"))

            self.assertFalse(form.is_valid())
            self.assertIn("postcode", form.errors)

        self.assertEqual(Site.objects.count(), 0)

    def test_admin_is_wired_to_the_geocoding_form(self, _queue):
        self.assertIs(SiteAdmin(Site, AdminSite()).form, SiteAdminForm)

    def test_unchanged_postcode_on_edit_skips_the_lookup(self, _queue):
        with patch("sites.forms.geocode_postcode", return_value=(55.95, -3.19)):
            site = SiteAdminForm(self._data()).save()

        with patch("sites.forms.geocode_postcode") as geo:
            form = SiteAdminForm(self._data(name="Renamed"), instance=site)
            self.assertTrue(form.is_valid(), form.errors)

        geo.assert_not_called()

    def test_portal_and_admin_forms_share_the_validation(self, _queue):
        """Both are built on the same mixin; a regression in one is both."""
        from sites.forms import GeocodedPostcodeMixin

        self.assertTrue(issubclass(SiteForm, GeocodedPostcodeMixin))
        self.assertTrue(issubclass(SiteAdminForm, GeocodedPostcodeMixin))
