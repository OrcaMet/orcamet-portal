"""
Tests for the bulk import views.

The importer's own rules are covered in test_importer.py. What is tested here
is the two-request flow around them: who may use it, that the preview writes
nothing, and that confirming creates what the preview showed rather than
whatever the browser last posted.
"""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from sites.models import Client, Site

COORDS = {
    "EH11YZ": (55.9510, -3.1890),
    "AB115DQ": (57.1430, -2.0810),
}


def fake_bulk(postcodes):
    from sites.models import normalise_postcode

    return {
        normalise_postcode(code): COORDS.get(normalise_postcode(code), (None, None))
        for code in postcodes if normalise_postcode(code)
    }


def make_user(self_service=True, role=User.Role.CLIENT_ADMIN, username="jo"):
    client = Client.objects.create(
        name=f"{username} Access Ltd",
        self_service_sites=self_service,
        site_limit=10,
        # Past onboarding, so nothing under test redirects into the wizard.
        onboarding_completed_at=timezone.now(),
    )
    return User.objects.create_user(
        username=username, email=f"{username}@example.com",
        role=role, client=client,
    )


@patch("sites.signals.queue_forecast_generation", return_value=True)
@patch("sites.importer.geocode_postcodes_bulk", side_effect=fake_bulk)
class ImportAccessTests(TestCase):
    def test_staff_managed_client_is_refused(self, _geo, _queue):
        self.client.force_login(make_user(self_service=False))
        self.assertEqual(self.client.get("/sites/import/").status_code, 403)

    def test_read_only_member_is_refused(self, _geo, _queue):
        self.client.force_login(make_user(role=User.Role.CLIENT_USER))
        self.assertEqual(self.client.get("/sites/import/").status_code, 403)

    def test_logged_out_goes_to_login(self, _geo, _queue):
        response = self.client.get("/sites/import/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_admin_of_a_self_service_workspace_gets_the_form(self, _geo, _queue):
        self.client.force_login(make_user())
        response = self.client.get("/sites/import/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Check these sites")


@patch("sites.signals.queue_forecast_generation", return_value=True)
@patch("sites.importer.geocode_postcodes_bulk", side_effect=fake_bulk)
class ImportFlowTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_preview_creates_nothing(self, _geo, _queue):
        response = self.client.post("/sites/import/", {
            "sites": "Tower A, EH1 1YZ\nTower B, AB11 5DQ",
            "preset": "rope_access",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2 sites ready to add")
        self.assertEqual(Site.objects.count(), 0)

    def test_preview_names_the_rows_it_cannot_use(self, _geo, _queue):
        response = self.client.post("/sites/import/", {
            "sites": "Tower A, EH1 1YZ\nTower B, XX99 9ZZ",
            "preset": "rope_access",
        })

        self.assertContains(response, "XX99 9ZZ")
        self.assertContains(response, "1 site ready to add")

    def test_confirm_creates_the_previewed_sites(self, _geo, _queue):
        self.client.post("/sites/import/", {
            "sites": "Tower A, EH1 1YZ\nTower B, AB11 5DQ",
            "preset": "crane_lifting",
        })

        response = self.client.post("/sites/import/confirm/", follow=True)

        self.assertEqual(Site.objects.filter(client=self.user.client).count(), 2)
        self.assertContains(response, "Added 2 sites")

    def test_the_success_message_is_honest_about_the_forecast_queue(self, _geo, _queue):
        """
        Only two runs go inline per worker; the rest wait for the cron. The
        message must not promise otherwise.
        """
        self.client.post("/sites/import/", {"sites": "Tower A, EH1 1YZ"})
        response = self.client.post("/sites/import/confirm/", follow=True)
        self.assertContains(response, "over the next hour")

    def test_confirm_without_a_preview_is_refused(self, _geo, _queue):
        response = self.client.post("/sites/import/confirm/", follow=True)

        self.assertEqual(Site.objects.count(), 0)
        self.assertContains(response, "had expired")

    def test_a_get_on_confirm_writes_nothing(self, _geo, _queue):
        self.client.post("/sites/import/", {"sites": "Tower A, EH1 1YZ"})

        response = self.client.get("/sites/import/confirm/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Site.objects.count(), 0)

    def test_the_plan_is_consumed_so_a_double_submit_creates_nothing_twice(self, _geo, _queue):
        self.client.post("/sites/import/", {"sites": "Tower A, EH1 1YZ"})
        self.client.post("/sites/import/confirm/")
        self.client.post("/sites/import/confirm/")

        self.assertEqual(Site.objects.count(), 1)

    def test_coordinates_come_from_the_lookup_not_the_browser(self, _geo, _queue):
        """
        The reason the plan lives in the session: a posted latitude must not
        be able to decide where a site is.
        """
        self.client.post("/sites/import/", {"sites": "Tower A, EH1 1YZ"})

        self.client.post("/sites/import/confirm/", {
            "latitude": "0.0", "longitude": "0.0",
        })

        site = Site.objects.get()
        self.assertAlmostEqual(site.latitude, 55.9510)

    def test_sites_land_in_the_posters_own_workspace(self, _geo, _queue):
        other = Client.objects.create(name="Someone Else")

        self.client.post("/sites/import/", {"sites": "Tower A, EH1 1YZ"})
        self.client.post("/sites/import/confirm/", {"client": other.pk})

        self.assertEqual(Site.objects.get().client, self.user.client)

    def test_an_empty_paste_is_reported(self, _geo, _queue):
        response = self.client.post("/sites/import/", {"sites": "   "}, follow=True)
        self.assertContains(response, "nothing to import")

    def test_onboarding_confirm_continues_the_wizard(self, _geo, _queue):
        self.client.post("/sites/import/?onboarding=1", {"sites": "Tower A, EH1 1YZ"})

        response = self.client.post("/sites/import/confirm/", {"onboarding": "1"})

        self.assertRedirects(
            response, "/onboarding/thresholds/", fetch_redirect_response=False
        )
