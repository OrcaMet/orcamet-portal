"""
A deactivated user is told so, rather than logged in.

django_login does not check is_active, but Django's session backend does on
the next request. The callback used to log a deactivated user in, the next
page bounced them to /login/, and Auth0 — its own session still valid —
signed them straight back in: an endless redirect loop.
"""

from unittest.mock import patch

from django.test import TestCase

from accounts.models import User


def _token(sub="auth0|old", email="old@example.com"):
    return {"userinfo": {
        "sub": sub, "email": email, "email_verified": True, "name": "Old Hand",
    }}


class InactiveLoginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="old", email="old@example.com",
            auth0_id="auth0|old", is_active=False,
        )

    def _callback(self, token):
        with patch("accounts.views.oauth") as mock_oauth:
            mock_oauth.auth0.authorize_access_token.return_value = token
            return self.client.get("/callback/?code=x&state=y")

    def test_deactivated_user_sees_the_deactivated_page(self):
        r = self._callback(_token())

        self.assertEqual(r.status_code, 403)
        self.assertContains(r, "deactivated", status_code=403)

    def test_deactivated_user_is_not_logged_in(self):
        self._callback(_token())

        self.assertNotIn("_auth_user_id", self.client.session)

    def test_matching_by_email_is_refused_too(self):
        """The email fallback must not route around the check."""
        self.user.auth0_id = None
        self.user.save()

        r = self._callback(_token(sub="auth0|someone-new"))

        self.assertEqual(r.status_code, 403)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_active_user_still_logs_in(self):
        self.user.is_active = True
        self.user.save()

        r = self._callback(_token())

        self.assertRedirects(r, "/dashboard/", fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
