"""A failed Auth0 token exchange must not show the exception to the visitor."""

from unittest.mock import patch

from django.test import TestCase


class LoginErrorTests(TestCase):
    def test_exception_detail_is_not_rendered(self):
        with patch("accounts.views.oauth") as mock_oauth:
            mock_oauth.auth0.authorize_access_token.side_effect = RuntimeError(
                "mismatching_state: CSRF Warning! internal-detail-xyz"
            )
            r = self.client.get("/callback/?code=abc&state=def")

        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "Login Error", status_code=400)
        self.assertNotContains(r, "internal-detail-xyz", status_code=400)
        self.assertContains(r, 'href="/login/"', status_code=400)
