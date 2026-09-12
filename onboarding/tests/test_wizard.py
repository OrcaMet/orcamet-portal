"""
Tests for the onboarding wizard.

Three properties matter: only the right people can reach it, it resumes
rather than restarting, and finishing it stops the dashboard redirecting
there forever.
"""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from sites.models import ChangeLog, Client, Site, ThresholdProfile
from sites.presets import thresholds_for


def make_workspace(self_service=True, role=User.Role.CLIENT_ADMIN,
                   username="jo", **client_kwargs):
    client = Client.objects.create(
        name=f"{username} Access Ltd",
        self_service_sites=self_service,
        site_limit=10,
        **client_kwargs,
    )
    user = User.objects.create_user(
        username=username, email=f"{username}@example.com",
        role=role, client=client,
    )
    return user


def add_site(client, name="Tower A", preset="rope_access"):
    site = Site.objects.create(
        client=client, name=name, postcode="EH11YZ",
        latitude=55.95, longitude=-3.19,
    )
    ThresholdProfile.objects.create(site=site, **thresholds_for(preset))
    return site


# The post_save signal would otherwise start a real forecast thread.
@patch("sites.signals.queue_forecast_generation", return_value=True)
class AccessTests(TestCase):
    def test_staff_managed_client_cannot_reach_the_wizard(self, _queue):
        """
        The guarantee behind leaving self-service off by default: a client
        whose sites OrcaMet manages is not offered a wizard that would let
        them change them.
        """
        user = make_workspace(self_service=False)
        self.client.force_login(user)

        response = self.client.get("/onboarding/welcome/")

        self.assertEqual(response.status_code, 403)

    def test_read_only_member_cannot_reach_the_wizard(self, _queue):
        user = make_workspace(role=User.Role.CLIENT_USER)
        self.client.force_login(user)

        response = self.client.get("/onboarding/operation/")

        self.assertEqual(response.status_code, 403)

    def test_logged_out_users_are_sent_to_login(self, _queue):
        response = self.client.get("/onboarding/welcome/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_admin_of_a_self_service_workspace_gets_in(self, _queue):
        user = make_workspace()
        self.client.force_login(user)

        response = self.client.get("/onboarding/welcome/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Welcome to OrcaMet")


@patch("sites.signals.queue_forecast_generation", return_value=True)
class DashboardRedirectTests(TestCase):
    def test_incomplete_onboarding_redirects_from_the_dashboard(self, _queue):
        user = make_workspace()
        self.client.force_login(user)

        response = self.client.get("/dashboard/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/onboarding/", response["Location"])

    def test_completed_onboarding_does_not_redirect(self, _queue):
        user = make_workspace()
        user.client.onboarding_completed_at = timezone.now()
        user.client.save()
        self.client.force_login(user)

        response = self.client.get("/dashboard/")

        self.assertEqual(response.status_code, 200)

    def test_staff_managed_client_is_never_redirected(self, _queue):
        user = make_workspace(self_service=False)
        self.client.force_login(user)

        response = self.client.get("/dashboard/")

        self.assertEqual(response.status_code, 200)

    def test_a_read_only_member_is_not_sent_to_a_wizard_they_cannot_use(self, _queue):
        """
        They would land on a 403 and have no way forward — worse than an
        empty dashboard.
        """
        client = Client.objects.create(name="Summit", self_service_sites=True)
        user = User.objects.create_user(
            username="sam", email="sam@example.com",
            role=User.Role.CLIENT_USER, client=client,
        )
        self.client.force_login(user)

        response = self.client.get("/dashboard/")

        self.assertEqual(response.status_code, 200)


@patch("sites.signals.queue_forecast_generation", return_value=True)
class ResumeTests(TestCase):
    """`start` sends people to the first thing they have not done."""

    def setUp(self):
        self.user = make_workspace()
        self.client.force_login(self.user)

    def test_a_brand_new_workspace_starts_at_welcome(self, _queue):
        response = self.client.get("/onboarding/")
        self.assertRedirects(response, "/onboarding/welcome/")

    def test_details_given_but_no_sites_resumes_at_sites(self, _queue):
        self.user.client.contact_email = "jo@example.com"
        self.user.client.save()

        response = self.client.get("/onboarding/")

        self.assertRedirects(response, "/onboarding/sites/")

    def test_sites_added_resumes_at_thresholds(self, _queue):
        self.user.client.contact_email = "jo@example.com"
        self.user.client.save()
        add_site(self.user.client)

        response = self.client.get("/onboarding/")

        self.assertRedirects(response, "/onboarding/thresholds/")

    def test_a_finished_workspace_goes_to_the_dashboard(self, _queue):
        self.user.client.onboarding_completed_at = timezone.now()
        self.user.client.save()

        response = self.client.get("/onboarding/")

        self.assertRedirects(response, "/dashboard/")

    def test_thresholds_without_a_site_sends_you_back_to_add_one(self, _queue):
        response = self.client.get("/onboarding/thresholds/")
        self.assertRedirects(response, "/onboarding/sites/")


@patch("sites.signals.queue_forecast_generation", return_value=True)
class OperationStepTests(TestCase):
    def setUp(self):
        self.user = make_workspace()
        self.client.force_login(self.user)

    def test_saves_contact_details_and_the_new_site_defaults(self, _queue):
        response = self.client.post("/onboarding/operation/", {
            "contact_name": "Jo Patel",
            "contact_email": "jo@summit.example",
            "contact_phone": "",
            "threshold_preset": "crane_lifting",
            "default_exposure": "coastal",
        })

        self.assertRedirects(response, "/onboarding/sites/")
        self.user.client.refresh_from_db()
        self.assertEqual(self.user.client.contact_name, "Jo Patel")
        # On the Client, not the session: these outlive onboarding and have
        # to be readable by the importer and the single-site form.
        self.assertEqual(self.user.client.threshold_preset, "crane_lifting")
        self.assertEqual(self.user.client.default_exposure, "coastal")

    def test_the_defaults_survive_a_new_session(self, _queue):
        """The regression this replaced: a logout used to lose the preset."""
        self.client.post("/onboarding/operation/", {
            "contact_name": "Jo Patel",
            "contact_email": "jo@summit.example",
            "threshold_preset": "crane_lifting",
            "default_exposure": "coastal",
        })

        self.client.logout()
        self.client.force_login(self.user)
        self.user.client.refresh_from_db()

        self.assertEqual(self.user.client.effective_preset, "crane_lifting")
        self.assertEqual(self.user.client.effective_exposure, "coastal")

    def test_a_bad_email_is_reported_rather_than_saved(self, _queue):
        response = self.client.post("/onboarding/operation/", {
            "contact_name": "Jo Patel",
            "contact_email": "not-an-email",
            "threshold_preset": "rope_access",
            "default_exposure": "urban",
        })

        self.assertEqual(response.status_code, 200)
        self.user.client.refresh_from_db()
        self.assertEqual(self.user.client.contact_name, "")


@patch("sites.signals.queue_forecast_generation", return_value=True)
class ThresholdStepTests(TestCase):
    def setUp(self):
        self.user = make_workspace()
        self.client.force_login(self.user)
        self.first = add_site(self.user.client, "Tower A")
        self.second = add_site(self.user.client, "Tower B")

    def _payload(self, **overrides):
        data = dict(thresholds_for("rope_access"))
        data.update(overrides)
        return {key: str(value) for key, value in data.items() if value is not None}

    def test_saving_applies_to_every_site_by_default(self, _queue):
        payload = self._payload(wind_mean_caution=6.5)
        payload["apply_to_all"] = "on"

        response = self.client.post("/onboarding/thresholds/", payload)

        self.assertRedirects(response, "/onboarding/done/")
        for site in (self.first, self.second):
            profile = ThresholdProfile.objects.get(site=site, is_active=True)
            self.assertEqual(profile.wind_mean_caution, 6.5)

    def test_each_site_keeps_exactly_one_active_profile(self, _queue):
        """
        Copying archives the old profile rather than editing it, so the
        history of what a past forecast was scored against survives.
        """
        payload = self._payload(wind_mean_caution=6.5)
        payload["apply_to_all"] = "on"
        self.client.post("/onboarding/thresholds/", payload)

        self.assertEqual(
            ThresholdProfile.objects.filter(site=self.second, is_active=True).count(), 1
        )
        self.assertEqual(
            ThresholdProfile.objects.filter(site=self.second).count(), 2
        )

    def test_copying_is_recorded_in_the_changelog(self, _queue):
        payload = self._payload(wind_mean_caution=6.5)
        payload["apply_to_all"] = "on"
        self.client.post("/onboarding/thresholds/", payload)

        entry = ChangeLog.objects.get(
            site=self.second, action=ChangeLog.Action.THRESHOLD_UPDATED
        )
        self.assertEqual(entry.details["source"], "onboarding")

    def test_unticking_leaves_the_other_sites_alone(self, _queue):
        payload = self._payload(wind_mean_caution=6.5)

        self.client.post("/onboarding/thresholds/", payload)

        other = ThresholdProfile.objects.get(site=self.second, is_active=True)
        self.assertEqual(
            other.wind_mean_caution, thresholds_for("rope_access")["wind_mean_caution"]
        )

    def test_an_inverted_pair_is_rejected(self, _queue):
        """ThresholdProfile.clean() guards the risk engine; it must run here."""
        payload = self._payload(wind_mean_caution=20.0, wind_mean_cancel=5.0)
        payload["apply_to_all"] = "on"

        response = self.client.post("/onboarding/thresholds/", payload)

        self.assertEqual(response.status_code, 200)
        profile = ThresholdProfile.objects.get(site=self.first, is_active=True)
        self.assertNotEqual(profile.wind_mean_cancel, 5.0)


@patch("sites.signals.queue_forecast_generation", return_value=True)
class CompletionTests(TestCase):
    def setUp(self):
        self.user = make_workspace()
        self.client.force_login(self.user)
        add_site(self.user.client)

    def test_get_on_done_does_not_complete_it(self, _queue):
        """A prefetch must not mark somebody's setup finished."""
        self.client.get("/onboarding/done/")

        self.user.client.refresh_from_db()
        self.assertFalse(self.user.client.onboarding_complete)

    def test_posting_done_completes_and_goes_to_the_dashboard(self, _queue):
        response = self.client.post("/onboarding/done/")

        self.assertRedirects(response, "/dashboard/")
        self.user.client.refresh_from_db()
        self.assertTrue(self.user.client.onboarding_complete)

    def test_skipping_stops_the_dashboard_redirect(self, _queue):
        self.client.post("/onboarding/skip/")

        response = self.client.get("/dashboard/")

        self.assertEqual(response.status_code, 200)

    def test_skip_refuses_a_get(self, _queue):
        response = self.client.get("/onboarding/skip/")
        self.assertEqual(response.status_code, 403)

    def test_completing_twice_keeps_the_original_timestamp(self, _queue):
        self.client.post("/onboarding/done/")
        self.user.client.refresh_from_db()
        first = self.user.client.onboarding_completed_at

        self.client.post("/onboarding/done/")
        self.user.client.refresh_from_db()

        self.assertEqual(self.user.client.onboarding_completed_at, first)
