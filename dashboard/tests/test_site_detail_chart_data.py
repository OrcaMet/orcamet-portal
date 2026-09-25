"""
The site detail page hands its chart data to the browser via json_script.

It used to be dumped with |safe straight into a <script> body, where any
value containing "</script>" would have ended the tag early. The page also
sent the browser internal run ids under a "debug" key.
"""

import json
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from forecasts.models import ForecastRun, HourlyForecast
from sites.models import Client, Site, ThresholdProfile


@patch("sites.signals.queue_forecast_generation", return_value=True)
class SiteDetailChartDataTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="Acme Rope")
        self.site = Site.objects.create(
            client=client, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )
        ThresholdProfile.objects.create(site=self.site, gust_cancel=21.0)
        user = User.objects.create_user(
            username="dave", role=User.Role.CLIENT_ADMIN, client=client,
        )
        self.client.force_login(user)

        run = ForecastRun.objects.create(
            site=self.site, forecast_date=timezone.localdate(),
            status=ForecastRun.Status.SUCCESS, peak_risk=12.0,
            recommendation="GO", models_used=["ukv"],
        )
        HourlyForecast.objects.create(
            run=run, timestamp=timezone.now(),
            wind_speed=5.0, wind_gusts=9.0, precipitation=0.0,
            temperature=11.0, hourly_risk=12.0,
        )

    def _chart_data(self, response):
        """Parse the payload out of the page, as the browser would."""
        html = response.content.decode()
        body = html.split('<script id="chart-data" type="application/json">')[1]
        return json.loads(body.split("</script>")[0])

    def test_chart_data_is_embedded_with_json_script(self, _q):
        r = self.client.get(f"/dashboard/site/{self.site.pk}/")

        self.assertContains(r, '<script id="chart-data" type="application/json">')
        self.assertNotContains(r, "chart_data_json")

    def test_chart_data_parses_and_carries_the_sites_limits(self, _q):
        data = self._chart_data(self.client.get(f"/dashboard/site/{self.site.pk}/"))

        self.assertEqual(len(data["hourly"]), 1)
        self.assertEqual(data["hourly"][0]["wind_gusts"], 9.0)
        self.assertEqual(data["thresholds"]["gust_cancel"], 21.0)

    def test_no_debug_payload_reaches_the_browser(self, _q):
        data = self._chart_data(self.client.get(f"/dashboard/site/{self.site.pk}/"))

        self.assertNotIn("debug", data)
