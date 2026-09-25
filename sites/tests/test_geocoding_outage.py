"""
A postcodes.io outage is not the user's postcode being wrong.

geocode_postcode used to return (None, None) for both, so while the service
was unreachable every trial user was told "We couldn't find that UK
postcode" about a perfectly good one.
"""

from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, TestCase

from sites.forms import SiteForm
from sites.models import Client, GeocodingUnavailable, Site, geocode_postcode


def _resp(status, payload=None):
    r = MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 400
    r.json.return_value = payload
    return r


class GeocodePostcodeTests(SimpleTestCase):
    def _call(self, **kwargs):
        with patch("requests.get", **kwargs):
            return geocode_postcode("LS1 4DY")

    def test_found(self):
        payload = {"status": 200, "result": {"latitude": 53.79, "longitude": -1.55}}
        self.assertEqual(self._call(return_value=_resp(200, payload)), (53.79, -1.55))

    def test_unknown_postcode_is_none(self):
        self.assertEqual(
            self._call(return_value=_resp(404, {"status": 404, "error": "Invalid postcode"})),
            (None, None),
        )

    def test_connection_failure_is_unavailable(self):
        with self.assertRaises(GeocodingUnavailable):
            self._call(side_effect=requests.ConnectionError("refused"))

    def test_timeout_is_unavailable(self):
        with self.assertRaises(GeocodingUnavailable):
            self._call(side_effect=requests.Timeout("slow"))

    def test_server_error_is_unavailable(self):
        with self.assertRaises(GeocodingUnavailable):
            self._call(return_value=_resp(503))

    def test_rate_limit_is_unavailable(self):
        with self.assertRaises(GeocodingUnavailable):
            self._call(return_value=_resp(429))

    def test_unreadable_body_is_unavailable(self):
        bad = _resp(200)
        bad.json.side_effect = ValueError("not json")
        with self.assertRaises(GeocodingUnavailable):
            self._call(return_value=bad)

    def test_non_object_body_is_unavailable(self):
        with self.assertRaises(GeocodingUnavailable):
            self._call(return_value=_resp(200, ["unexpected"]))


@patch("sites.signals.queue_forecast_generation", return_value=True)
class OutageInFormsTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Pat (Sandbox)", is_sandbox=True)

    def _form(self):
        return SiteForm(
            data={"name": "Leeds Mast", "postcode": "LS1 4DY", "exposure": "urban",
                  "elevation": 0, "notes": ""},
            client=self.client_obj,
        )

    def test_outage_says_the_service_is_down(self, _q):
        with patch("sites.forms.geocode_postcode", side_effect=GeocodingUnavailable("down")):
            form = self._form()
            self.assertFalse(form.is_valid())

        message = " ".join(form.errors["postcode"])
        self.assertIn("isn't responding", message)
        self.assertNotIn("couldn't find", message)

    def test_unknown_postcode_still_says_so(self, _q):
        with patch("sites.forms.geocode_postcode", return_value=(None, None)):
            form = self._form()
            self.assertFalse(form.is_valid())

        self.assertIn("couldn't find", " ".join(form.errors["postcode"]))

    def test_geocode_if_needed_reports_an_outage_as_failure(self, _q):
        site = Site(client=self.client_obj, name="Tower", postcode="LS1 4DY")
        with patch("sites.models.geocode_postcode", side_effect=GeocodingUnavailable("down")):
            self.assertFalse(site.geocode_if_needed())
        self.assertIsNone(site.latitude)
