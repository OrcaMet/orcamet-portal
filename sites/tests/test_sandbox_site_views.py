"""
Tests for self-service site management by sandbox (trial) accounts.

The important properties are that a tester cannot reach outside their own
workspace, and that the site cap actually holds — every site created fires a
live Open-Meteo forecast run.
"""

from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings

from accounts.models import User
from sites.models import Client, Site, ThresholdProfile


def make_user(sandbox=True, role=User.Role.CLIENT_ADMIN, username="dave"):
    client = Client.objects.create(name=f"{username} Co", is_sandbox=sandbox)
    return User.objects.create_user(
        username=username, email=f"{username}@example.com",
        role=role, client=client,
    )


# The post_save signal spawns a background thread that calls Open-Meteo.
# Patched out everywhere so the suite never makes network calls.
@patch("sites.signals.queue_forecast_generation", return_value=True)
@patch("sites.models.geocode_postcode", return_value=(55.95, -3.19))
class SandboxSiteCreateTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def _post(self, **overrides):
        data = {
            "name": "Tower Block A",
            "postcode": "EH1 1YZ",
            "exposure": "urban",
            "elevation": 0,
            "notes": "",
        }
        data.update(overrides)
        return self.client.post("/sites/add/", data)

    def test_creates_site_against_own_client(self, _geo, _queue):
        with patch("sites.forms.geocode_postcode", return_value=(55.95, -3.19)):
            self._post()

        site = Site.objects.get()
        self.assertEqual(site.client, self.user.client)
        self.assertEqual(site.latitude, 55.95)

    def test_creates_an_active_threshold_profile(self, _geo, _queue):
        """Without one the forecast engine has no limits to score against."""
        with patch("sites.forms.geocode_postcode", return_value=(55.95, -3.19)):
            self._post()

        profile = ThresholdProfile.objects.get()
        self.assertEqual(profile.site, Site.objects.get())
        self.assertTrue(profile.is_active)

    def test_posted_client_id_is_ignored(self, _geo, _queue):
        """A tester must not be able to attach a site to another workspace."""
        victim = Client.objects.create(name="Real Client Ltd")

        with patch("sites.forms.geocode_postcode", return_value=(55.95, -3.19)):
            self._post(client=victim.pk)

        self.assertEqual(Site.objects.get().client, self.user.client)
        self.assertFalse(Site.objects.filter(client=victim).exists())

    def test_ungeocodable_postcode_is_rejected_with_a_message(self, _geo, _queue):
        with patch("sites.forms.geocode_postcode", return_value=(None, None)):
            response = self._post(postcode="ZZ99 9ZZ")

        self.assertEqual(Site.objects.count(), 0)
        self.assertContains(response, "couldn&#x27;t find that UK postcode")

    def test_duplicate_name_is_rejected_not_a_500(self, _geo, _queue):
        Site.objects.create(client=self.user.client, name="Tower Block A", postcode="EH1 1YZ")

        with patch("sites.forms.geocode_postcode", return_value=(55.95, -3.19)):
            response = self._post()

        self.assertContains(response, "already have a site with that name")
        self.assertEqual(Site.objects.count(), 1)

    @override_settings(SANDBOX_MAX_SITES=2)
    def test_cap_blocks_creating_beyond_the_limit(self, _geo, _queue):
        for i in range(2):
            Site.objects.create(client=self.user.client, name=f"S{i}", postcode="EH1 1YZ")

        with patch("sites.forms.geocode_postcode", return_value=(55.95, -3.19)):
            response = self._post()

        self.assertRedirects(response, "/dashboard/", fetch_redirect_response=False)
        self.assertEqual(Site.objects.count(), 2)

    @override_settings(SANDBOX_MAX_SITES=1)
    def test_removed_site_frees_a_slot(self, _geo, _queue):
        Site.objects.create(
            client=self.user.client, name="Old", postcode="EH1 1YZ", is_active=False,
        )

        with patch("sites.forms.geocode_postcode", return_value=(55.95, -3.19)):
            self._post()

        self.assertEqual(Site.objects.filter(is_active=True).count(), 1)


@patch("sites.signals.queue_forecast_generation", return_value=True)
@patch("sites.models.geocode_postcode", return_value=(55.95, -3.19))
class SandboxAccessControlTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.other = make_user(username="mallory")
        self.other_site = Site.objects.create(
            client=self.other.client, name="Their Site", postcode="EH1 1YZ",
        )

    def test_anonymous_users_are_sent_to_login(self, _geo, _queue):
        response = self.client.get("/sites/add/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_non_sandbox_user_is_forbidden(self, _geo, _queue):
        """Real client sites stay staff-managed."""
        real = make_user(sandbox=False, username="realclient")
        self.client.force_login(real)

        self.assertEqual(self.client.get("/sites/add/").status_code, 403)

    def test_cannot_edit_another_workspaces_site(self, _geo, _queue):
        self.client.force_login(self.user)
        response = self.client.get(f"/sites/{self.other_site.pk}/edit/")
        self.assertEqual(response.status_code, 404)

    def test_cannot_remove_another_workspaces_site(self, _geo, _queue):
        self.client.force_login(self.user)
        response = self.client.post(f"/sites/{self.other_site.pk}/remove/")

        self.assertEqual(response.status_code, 404)
        self.other_site.refresh_from_db()
        self.assertTrue(self.other_site.is_active)


@patch("sites.signals.queue_forecast_generation", return_value=True)
@patch("sites.models.geocode_postcode", return_value=(55.95, -3.19))
class SandboxForecastOrderingTests(TransactionTestCase):
    """
    TransactionTestCase, not TestCase: the ordering under test depends on a
    real commit. TestCase wraps each test in a transaction that never commits,
    so transaction.on_commit callbacks would not fire at all and this would
    pass whether or not the view is correct.
    """

    def test_forecast_is_queued_only_after_thresholds_exist(self, _geo, _queue):
        """
        The post_save signal queues the forecast run via transaction.on_commit,
        and ATOMIC_REQUESTS is off — so unless the view opens its own
        transaction, the run starts as soon as the site row commits, before
        the ThresholdProfile lands, and the runner silently scores that first
        forecast against its hardcoded fallback limits.
        """
        user = make_user(username="ordering")
        self.client.force_login(user)

        seen = {}

        def record(site_id, site_name=""):
            seen["thresholds"] = ThresholdProfile.objects.filter(
                site_id=site_id, is_active=True
            ).exists()
            return True

        _queue.side_effect = record

        with patch("sites.forms.geocode_postcode", return_value=(55.95, -3.19)):
            self.client.post("/sites/add/", {
                "name": "Tower Block A", "postcode": "EH1 1YZ",
                "exposure": "urban", "elevation": 0, "notes": "",
            })

        self.assertTrue(_queue.called, "forecast generation was never queued")
        self.assertTrue(
            seen["thresholds"],
            "forecast run was queued before the site's thresholds existed",
        )


@patch("sites.signals.queue_forecast_generation", return_value=True)
@patch("sites.models.geocode_postcode", return_value=(55.95, -3.19))
class SandboxDashboardTests(TestCase):
    """The dashboard grew sandbox-only branches; make sure they render."""

    @override_settings(SANDBOX_MAX_SITES=3)
    def test_empty_sandbox_dashboard_offers_the_add_link(self, _geo, _queue):
        user = make_user()
        self.client.force_login(user)

        response = self.client.get("/dashboard/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/sites/add/")
        self.assertContains(response, "using 0 of 3 sites")

    @override_settings(SANDBOX_MAX_SITES=1)
    def test_dashboard_hides_add_link_once_capped(self, _geo, _queue):
        user = make_user()
        Site.objects.create(client=user.client, name="S", postcode="EH1 1YZ")
        self.client.force_login(user)

        response = self.client.get("/dashboard/")

        self.assertNotContains(response, "/sites/add/")
        self.assertContains(response, "Remove a site to add another")

    def test_real_client_dashboard_has_no_sandbox_controls(self, _geo, _queue):
        user = make_user(sandbox=False, username="realclient")
        self.client.force_login(user)

        response = self.client.get("/dashboard/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "/sites/add/")
        self.assertContains(response, "Contact your OrcaMet account manager")

    def test_sandbox_site_detail_offers_the_edit_link(self, _geo, _queue):
        user = make_user()
        site = Site.objects.create(
            client=user.client, name="Mine", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )
        self.client.force_login(user)

        response = self.client.get(f"/dashboard/site/{site.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"/sites/{site.pk}/edit/")


@patch("sites.signals.queue_forecast_generation", return_value=True)
@patch("sites.models.geocode_postcode", return_value=(55.95, -3.19))
class SandboxSiteEditDeleteTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.site = Site.objects.create(
            client=self.user.client, name="Mine", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )

    def test_changing_postcode_regeocodes(self, _geo, _queue):
        """Site.save() only geocodes when lat/lon are None, so the form must."""
        with patch("sites.forms.geocode_postcode", return_value=(51.50, -0.12)) as geo:
            self.client.post(f"/sites/{self.site.pk}/edit/", {
                "name": "Mine", "postcode": "SW1A 1AA",
                "exposure": "urban", "elevation": 0, "notes": "",
            })

        geo.assert_called_once()
        self.site.refresh_from_db()
        self.assertEqual(self.site.latitude, 51.50)

    def test_unchanged_postcode_skips_the_geocode_call(self, _geo, _queue):
        with patch("sites.forms.geocode_postcode") as geo:
            self.client.post(f"/sites/{self.site.pk}/edit/", {
                "name": "Renamed", "postcode": "EH1 1YZ",
                "exposure": "urban", "elevation": 0, "notes": "",
            })

        geo.assert_not_called()
        self.site.refresh_from_db()
        self.assertEqual(self.site.name, "Renamed")

    def test_remove_deactivates_rather_than_deletes(self, _geo, _queue):
        self.client.post(f"/sites/{self.site.pk}/remove/")

        self.site.refresh_from_db()
        self.assertFalse(self.site.is_active)

    def test_get_on_remove_shows_a_confirmation_first(self, _geo, _queue):
        response = self.client.get(f"/sites/{self.site.pk}/remove/")

        self.assertEqual(response.status_code, 200)
        self.site.refresh_from_db()
        self.assertTrue(self.site.is_active)
