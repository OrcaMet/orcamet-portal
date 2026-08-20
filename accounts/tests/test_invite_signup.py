"""
Tests for invite-based signup.

The gate these cover is the one that decides whether a stranger who
authenticates at Auth0 gets an account at all, so the negative cases matter
more than the happy path.
"""

from datetime import timedelta
from unittest.mock import patch

from django.http import HttpResponseRedirect
from django.test import TestCase
from django.utils import timezone

from accounts.models import Invite, User
from accounts.provisioning import (
    SESSION_KEY,
    lookup_invite,
    provision_sandbox_user,
)
from sites.models import Client


def make_invite(**kwargs):
    kwargs.setdefault("label", "Test invite")
    return Invite.objects.create(**kwargs)


class LookupInviteTests(TestCase):
    def test_usable_invite_is_returned(self):
        invite = make_invite()
        self.assertEqual(lookup_invite(invite.code), invite)

    def test_unknown_code_returns_none(self):
        make_invite()
        self.assertIsNone(lookup_invite("not-a-real-code"))

    def test_blank_and_non_string_codes_return_none(self):
        for value in ("", None, 12345, []):
            self.assertIsNone(lookup_invite(value))

    def test_revoked_invite_is_rejected(self):
        invite = make_invite(is_active=False)
        self.assertIsNone(lookup_invite(invite.code))

    def test_expired_invite_is_rejected(self):
        invite = make_invite(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertIsNone(lookup_invite(invite.code))

    def test_exhausted_invite_is_rejected(self):
        invite = make_invite(max_uses=1)
        Invite.objects.filter(pk=invite.pk).update(uses=1)
        self.assertIsNone(lookup_invite(invite.code))

    def test_zero_max_uses_means_unlimited(self):
        invite = make_invite(max_uses=0)
        Invite.objects.filter(pk=invite.pk).update(uses=500)
        self.assertIsNotNone(lookup_invite(invite.code))

    def test_codes_are_unique_and_not_sequential(self):
        codes = {make_invite().code for _ in range(20)}
        self.assertEqual(len(codes), 20)
        self.assertTrue(all(len(c) >= 12 for c in codes))


class ProvisionSandboxUserTests(TestCase):
    def test_creates_user_with_own_sandbox_client(self):
        invite = make_invite()
        user = provision_sandbox_user(invite, "auth0|abc", "dave@example.com", "Dave Smith")

        self.assertIsNotNone(user)
        self.assertEqual(user.email, "dave@example.com")
        self.assertEqual(user.first_name, "Dave")
        self.assertEqual(user.last_name, "Smith")
        self.assertEqual(user.auth0_id, "auth0|abc")
        self.assertEqual(user.role, User.Role.CLIENT_ADMIN)
        self.assertTrue(user.client.is_sandbox)
        self.assertTrue(user.is_sandbox_user)

    def test_account_has_no_usable_django_password(self):
        """The account must only be reachable through Auth0."""
        invite = make_invite()
        user = provision_sandbox_user(invite, "auth0|abc", "dave@example.com", "Dave")
        self.assertFalse(user.has_usable_password())

    def test_usage_count_is_incremented(self):
        invite = make_invite(max_uses=2)
        provision_sandbox_user(invite, "auth0|a", "a@example.com", "A")
        invite.refresh_from_db()
        self.assertEqual(invite.uses, 1)

    def test_invite_revoked_after_the_click_is_refused(self):
        """
        The usable check in signup_view runs before a round trip to Auth0.
        Provisioning must re-check rather than trust that earlier result.
        """
        invite = make_invite()
        Invite.objects.filter(pk=invite.pk).update(is_active=False)

        user = provision_sandbox_user(invite, "auth0|a", "a@example.com", "A")

        self.assertIsNone(user)
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(Client.objects.count(), 0)

    def test_second_use_of_single_use_invite_is_refused(self):
        invite = make_invite(max_uses=1)
        first = provision_sandbox_user(invite, "auth0|a", "a@example.com", "A")
        second = provision_sandbox_user(invite, "auth0|b", "b@example.com", "B")

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(User.objects.count(), 1)

    def test_multi_use_invite_creates_separate_workspaces(self):
        invite = make_invite(max_uses=3)
        a = provision_sandbox_user(invite, "auth0|a", "dave@example.com", "Dave")
        b = provision_sandbox_user(invite, "auth0|b", "dave@other.com", "Dave")

        self.assertNotEqual(a.client_id, b.client_id)
        self.assertNotEqual(a.username, b.username)

    def test_colliding_usernames_are_made_unique(self):
        User.objects.create_user(username="dave", email="existing@example.com")
        invite = make_invite()
        user = provision_sandbox_user(invite, "auth0|a", "dave@example.com", "Dave")
        self.assertNotEqual(user.username, "dave")


class SignupViewTests(TestCase):
    def test_valid_invite_parks_code_and_redirects_to_auth0(self):
        invite = make_invite()
        with patch("accounts.views.oauth") as mock_oauth:
            mock_oauth.auth0.authorize_redirect.return_value = HttpResponseRedirect(
                "https://auth0.example.com/authorize"
            )
            response = self.client.get("/signup/", {"invite": invite.code})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session[SESSION_KEY], invite.code)
        # Auth0 should land them on its sign-up tab, not its login tab.
        _, kwargs = mock_oauth.auth0.authorize_redirect.call_args
        self.assertEqual(kwargs.get("screen_hint"), "signup")

    def test_invalid_invite_is_refused_without_touching_auth0(self):
        with patch("accounts.views.oauth") as mock_oauth:
            response = self.client.get("/signup/", {"invite": "nope"})

        self.assertEqual(response.status_code, 404)
        mock_oauth.auth0.authorize_redirect.assert_not_called()
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_missing_invite_param_is_refused(self):
        with patch("accounts.views.oauth"):
            response = self.client.get("/signup/")
        self.assertEqual(response.status_code, 404)


class CallbackProvisioningTests(TestCase):
    """The callback is the actual trust boundary."""

    def _callback(self, session_code=None, email_verified=True,
                  email="dave@example.com", sub="auth0|new"):
        if session_code is not None:
            session = self.client.session
            session[SESSION_KEY] = session_code
            session.save()

        token = {"userinfo": {
            "sub": sub,
            "email": email,
            "name": "Dave Smith",
            "email_verified": email_verified,
        }}
        with patch("accounts.views.oauth") as mock_oauth:
            mock_oauth.auth0.authorize_access_token.return_value = token
            return self.client.get("/callback/")

    def test_no_invite_still_denies_access(self):
        """Unchanged behaviour: a stranger with no invite gets nothing."""
        response = self._callback()
        self.assertContains(response, "Access Not Configured")
        self.assertEqual(User.objects.count(), 0)

    def test_valid_invite_creates_the_account_and_logs_in(self):
        invite = make_invite()
        response = self._callback(session_code=invite.code)

        self.assertRedirects(response, "/dashboard/", fetch_redirect_response=False)
        user = User.objects.get()
        self.assertTrue(user.is_sandbox_user)
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_unverified_email_is_refused_provisioning(self):
        """
        Otherwise someone could claim an invite under an address they don't
        own — which the email-matching branch would then honour on a later
        login, handing them that account.
        """
        invite = make_invite()
        response = self._callback(session_code=invite.code, email_verified=False)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(User.objects.count(), 0)
        invite.refresh_from_db()
        self.assertEqual(invite.uses, 0)

    def test_invite_survives_so_signup_completes_after_verifying(self):
        """
        Auth0 returns the user here immediately after sign-up, before they
        have clicked the verification email — so the refusal above is the
        normal path. The held invite lets a plain login finish the job.
        """
        invite = make_invite()

        self._callback(session_code=invite.code, email_verified=False)
        self.assertEqual(User.objects.count(), 0)

        # They verify, then log in again through the ordinary login button.
        response = self._callback(email_verified=True)

        self.assertRedirects(response, "/dashboard/", fetch_redirect_response=False)
        user = User.objects.get()
        self.assertTrue(user.is_sandbox_user)

    def test_held_invite_still_refuses_an_unverified_retry(self):
        """Holding the code must not let a second attempt skip the check."""
        invite = make_invite()

        self._callback(session_code=invite.code, email_verified=False)
        response = self._callback(email_verified=False)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(User.objects.count(), 0)

    def test_forged_invite_code_in_session_is_refused(self):
        response = self._callback(session_code="made-up-code")
        self.assertContains(response, "Access Not Configured")
        self.assertEqual(User.objects.count(), 0)

    def test_invite_is_consumed_so_it_cannot_be_replayed(self):
        invite = make_invite(max_uses=1)
        self._callback(session_code=invite.code)
        self.assertNotIn(SESSION_KEY, self.client.session)
        invite.refresh_from_db()
        self.assertEqual(invite.uses, 1)

    def test_existing_user_login_is_unaffected_by_a_stale_invite(self):
        """An invite in the session must not re-provision an existing user."""
        invite = make_invite()
        existing = User.objects.create_user(
            username="dave", email="dave@example.com", auth0_id="auth0|old",
        )

        response = self._callback(session_code=invite.code, sub="auth0|old")

        self.assertRedirects(response, "/dashboard/", fetch_redirect_response=False)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(self.client.session["_auth_user_id"], str(existing.pk))
        invite.refresh_from_db()
        self.assertEqual(invite.uses, 0)
