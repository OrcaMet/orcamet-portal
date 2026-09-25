"""
Site edits and removals by sandbox users, and which saves re-forecast.

- A removed site no longer blocks its own name.
- The site cap is re-checked under a lock, so two submissions at once cannot
  both slip past it.
- Only changes a forecast depends on queue a new forecast run.
"""

from unittest.mock import patch

from django.test import TestCase

from accounts.models import User
from sites.models import Client, Site


def make_user():
    client = Client.objects.create(name="Dave Co", is_sandbox=True)
    return User.objects.create_user(
        username="dave", email="dave@example.com",
        role=User.Role.CLIENT_ADMIN, client=client,
    )


FORM = {
    "name": "Tower Block A", "postcode": "EH1 1YZ",
    "exposure": "urban", "elevation": 0, "notes": "",
}


@patch("sites.signals.queue_forecast_generation", return_value=True)
@patch("sites.forms.geocode_postcode", return_value=(55.95, -3.19))
class RemovedSiteNameTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_a_removed_sites_name_can_be_used_again(self, _geo, _q):
        self.client.post("/sites/add/", FORM)
        old = Site.objects.get()
        self.client.post(f"/sites/{old.pk}/remove/")

        r = self.client.post("/sites/add/", FORM)

        new = Site.objects.get(is_active=True)
        self.assertRedirects(r, f"/dashboard/site/{new.pk}/", fetch_redirect_response=False)
        self.assertEqual(new.name, "Tower Block A")
        old.refresh_from_db()
        self.assertFalse(old.is_active)
        self.assertEqual(old.name, f"Tower Block A [removed #{old.pk}]")

    def test_an_active_site_still_blocks_its_name(self, _geo, _q):
        self.client.post("/sites/add/", FORM)

        r = self.client.post("/sites/add/", FORM)

        self.assertContains(r, "already have a site with that name")
        self.assertEqual(Site.objects.count(), 1)

    def test_renaming_onto_a_removed_sites_name(self, _geo, _q):
        self.client.post("/sites/add/", FORM)
        old = Site.objects.get()
        self.client.post(f"/sites/{old.pk}/remove/")
        self.client.post("/sites/add/", dict(FORM, name="Other"))
        other = Site.objects.get(is_active=True)

        self.client.post(f"/sites/{other.pk}/edit/", FORM)

        other.refresh_from_db()
        self.assertEqual(other.name, "Tower Block A")

    def test_a_long_name_is_truncated_to_fit_the_suffix(self, _geo, _q):
        long_name = "x" * 200
        self.client.post("/sites/add/", dict(FORM, name=long_name))
        old = Site.objects.get()
        self.client.post(f"/sites/{old.pk}/remove/")

        self.client.post("/sites/add/", dict(FORM, name=long_name))

        old.refresh_from_db()
        self.assertLessEqual(len(old.name), 200)
        self.assertTrue(old.name.endswith(f"[removed #{old.pk}]"))


@patch("sites.signals.queue_forecast_generation", return_value=True)
@patch("sites.forms.geocode_postcode", return_value=(55.95, -3.19))
class SiteCapRecheckTests(TestCase):
    def test_cap_is_rechecked_inside_the_transaction(self, _geo, _q):
        """
        Simulates the race: the first check passes, but by the time the
        client row is locked another submission has taken the last slot.
        """
        user = make_user()
        self.client.force_login(user)

        with patch(
            "sites.views._site_allowance",
            side_effect=[(0, 3, True), (3, 3, False)],
        ):
            r = self.client.post("/sites/add/", FORM)

        self.assertRedirects(r, "/dashboard/", fetch_redirect_response=False)
        self.assertEqual(Site.objects.count(), 0)


@patch("sites.signals.queue_forecast_generation", return_value=True)
class ReforecastOnlyOnRelevantChangeTests(TestCase):
    def setUp(self):
        # Class-level @patch does not cover setUp, so the on-commit callback
        # from this create must not run — TestCase discards it uncaptured.
        client = Client.objects.create(name="Acme Rope")
        self.site = Site.objects.create(
            client=client, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )

    def _save(self, **changes):
        for field, value in changes.items():
            setattr(self.site, field, value)
        with self.captureOnCommitCallbacks(execute=True):
            self.site.save()

    def test_creating_a_site_forecasts(self, queue):
        client = Client.objects.create(name="Other")
        with self.captureOnCommitCallbacks(execute=True):
            Site.objects.create(
                client=client, name="New", postcode="EH1 1YZ",
                latitude=55.95, longitude=-3.19,
            )
        queue.assert_called_once()

    def test_renaming_does_not_forecast(self, queue):
        self._save(name="Tower B", notes="scaffold on east face", elevation=40)
        queue.assert_not_called()

    def test_moving_the_site_forecasts(self, queue):
        self._save(postcode="G1 1AA", latitude=55.86, longitude=-4.25)
        queue.assert_called_once()

    def test_changing_exposure_forecasts(self, queue):
        self._save(exposure="coastal")
        queue.assert_called_once()

    def test_reopening_a_completed_job_forecasts(self, queue):
        self._save(job_complete=True)
        queue.reset_mock()

        self._save(job_complete=False)

        queue.assert_called_once()

    def test_update_fields_without_forecast_inputs_does_not_forecast(self, queue):
        self.site.name = "Renamed"
        with self.captureOnCommitCallbacks(execute=True):
            self.site.save(update_fields=["name"])
        queue.assert_not_called()
