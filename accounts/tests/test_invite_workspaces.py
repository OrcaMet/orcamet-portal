"""
Tests for the three kinds of workspace an invite can lead to.

The existing test_invite_signup.py covers the trial path and the trust gate
around it. This covers what was added for onboarding a real client: naming a
workspace up front, joining one that already exists, and — the one that
matters most — that none of it hands self-service to a staff-managed client
that never had it.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import Invite, User
from accounts.provisioning import provision_user_from_invite
from sites.models import Client


def make_invite(**kwargs):
    kwargs.setdefault("label", "Test invite")
    return Invite.objects.create(**kwargs)


class RealClientInviteTests(TestCase):
    def setUp(self):
        self.invite = make_invite(
            creates_sandbox=False,
            client_name="Summit Rope Access Ltd",
            site_limit=25,
        )

    def test_creates_a_named_non_sandbox_workspace(self):
        user = provision_user_from_invite(
            self.invite, "auth0|abc", "ops@summit.example", "Jo Patel"
        )

        self.assertEqual(user.client.name, "Summit Rope Access Ltd")
        self.assertFalse(user.client.is_sandbox)
        self.assertEqual(user.client.site_limit, 25)

    def test_the_workspace_may_manage_its_own_sites(self):
        """The whole point of the invite: they set themselves up."""
        user = provision_user_from_invite(
            self.invite, "auth0|abc", "ops@summit.example", "Jo Patel"
        )
        self.assertTrue(user.client.self_service_sites)
        self.assertEqual(user.role, User.Role.CLIENT_ADMIN)

    def test_onboarding_starts_incomplete(self):
        user = provision_user_from_invite(
            self.invite, "auth0|abc", "ops@summit.example", "Jo Patel"
        )
        self.assertFalse(user.client.onboarding_complete)

    def test_a_clashing_client_name_is_disambiguated(self):
        Client.objects.create(name="Summit Rope Access Ltd")
        user = provision_user_from_invite(
            self.invite, "auth0|abc", "ops@summit.example", "Jo Patel"
        )
        self.assertNotEqual(user.client.name, "Summit Rope Access Ltd")
        self.assertIn("Summit Rope Access Ltd", user.client.name)

    def test_sandbox_invite_still_makes_a_sandbox(self):
        invite = make_invite()  # creates_sandbox defaults True
        user = provision_user_from_invite(
            invite, "auth0|xyz", "dave@example.com", "Dave"
        )
        self.assertTrue(user.client.is_sandbox)
        self.assertIn("(Sandbox)", user.client.name)


class JoinExistingClientTests(TestCase):
    def setUp(self):
        self.existing = Client.objects.create(
            name="Summit Rope Access Ltd",
            self_service_sites=True,
            site_limit=25,
        )
        self.invite = make_invite(
            creates_sandbox=False,
            existing_client=self.existing,
            granted_role=User.Role.CLIENT_USER,
            max_uses=0,
        )

    def test_joins_without_creating_a_workspace(self):
        before = Client.objects.count()
        user = provision_user_from_invite(
            self.invite, "auth0|abc", "colleague@summit.example", "Sam Reid"
        )

        self.assertEqual(Client.objects.count(), before)
        self.assertEqual(user.client, self.existing)

    def test_honours_the_role_on_the_invite(self):
        user = provision_user_from_invite(
            self.invite, "auth0|abc", "colleague@summit.example", "Sam Reid"
        )
        self.assertEqual(user.role, User.Role.CLIENT_USER)
        self.assertFalse(user.can_edit_thresholds)

    def test_does_not_raise_the_clients_site_limit(self):
        """A new colleague is not extra allowance."""
        provision_user_from_invite(
            self.invite, "auth0|abc", "colleague@summit.example", "Sam Reid"
        )
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.site_limit, 25)

    def test_joining_a_staff_managed_client_does_not_grant_self_service(self):
        """
        The regression that would quietly undo "keep sites staff-managed":
        an invite into an existing client must change nothing about it.
        """
        staff_managed = Client.objects.create(name="Acme Access")
        invite = make_invite(creates_sandbox=False, existing_client=staff_managed)

        user = provision_user_from_invite(
            invite, "auth0|abc", "someone@acme.example", "Chris Bell"
        )

        staff_managed.refresh_from_db()
        self.assertFalse(staff_managed.self_service_sites)
        self.assertEqual(user.client, staff_managed)

    def test_several_colleagues_on_one_unlimited_invite(self):
        for index in range(3):
            provision_user_from_invite(
                self.invite, f"auth0|{index}",
                f"person{index}@summit.example", f"Person {index}",
            )

        self.assertEqual(self.existing.users.count(), 3)
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.uses, 3)


class InviteValidationTests(TestCase):
    """
    Configurations that would do something other than the field names promise
    are refused at the admin, not honoured silently.
    """

    def test_real_client_invite_needs_a_name(self):
        invite = Invite(label="x", creates_sandbox=False)
        with self.assertRaises(ValidationError) as raised:
            invite.full_clean()
        self.assertIn("client_name", raised.exception.error_dict)

    def test_joining_invite_must_not_also_create_a_sandbox(self):
        client = Client.objects.create(name="Summit")
        invite = Invite(label="x", creates_sandbox=True, existing_client=client)
        with self.assertRaises(ValidationError) as raised:
            invite.full_clean()
        self.assertIn("creates_sandbox", raised.exception.error_dict)

    def test_joining_invite_must_not_set_a_site_limit(self):
        client = Client.objects.create(name="Summit")
        invite = Invite(
            label="x", creates_sandbox=False, existing_client=client,
            site_limit=10,
        )
        with self.assertRaises(ValidationError) as raised:
            invite.full_clean()
        self.assertIn("site_limit", raised.exception.error_dict)

    def test_a_plain_sandbox_invite_validates(self):
        Invite(label="x").full_clean()  # must not raise

    def test_a_well_formed_real_client_invite_validates(self):
        Invite(
            label="x", creates_sandbox=False,
            client_name="Summit Rope Access Ltd", site_limit=25,
        ).full_clean()  # must not raise
