"""
Smoke tests for the Invite admin.

Custom fieldsets and readonly_fields fail at render time, not import time, so
a typo here would only show up when Steve opens the page in production.
"""

from django.test import TestCase
from django.urls import reverse

from accounts.models import Invite, User


class InviteAdminTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(
            username="steve", email="steve@example.com", password="test-password",
        )
        self.client.force_login(self.staff)

    def test_changelist_renders(self):
        Invite.objects.create(label="Dave's trial")
        response = self.client.get(reverse("admin:accounts_invite_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dave&#x27;s trial")
        self.assertContains(response, "Usable")

    def test_add_form_renders(self):
        response = self.client.get(reverse("admin:accounts_invite_add"))
        self.assertEqual(response.status_code, 200)

    def test_change_form_shows_the_signup_link(self):
        invite = Invite.objects.create(label="Dave's trial")
        response = self.client.get(
            reverse("admin:accounts_invite_change", args=[invite.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"/signup/?invite={invite.code}")

    def test_creating_an_invite_records_the_author_and_shows_the_link(self):
        response = self.client.post(
            reverse("admin:accounts_invite_add"),
            {"label": "New trial", "is_active": "on", "max_uses": 1},
            follow=True,
        )

        invite = Invite.objects.get(label="New trial")
        self.assertEqual(invite.created_by, self.staff)
        self.assertContains(response, f"/signup/?invite={invite.code}")

    def test_status_reflects_revocation(self):
        Invite.objects.create(label="Revoked one", is_active=False)
        response = self.client.get(reverse("admin:accounts_invite_changelist"))
        self.assertContains(response, "Revoked")
