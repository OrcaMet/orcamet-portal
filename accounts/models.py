"""
OrcaMet Portal — User Model

Three roles:
  - superadmin: OrcaMet staff (Steve). Full access to everything.
  - client_admin: Client company manager. Can edit thresholds for their sites.
  - client_user: Read-only access to their client's sites and forecasts.

Also holds Invite, which is how test accounts are self-provisioned — see
accounts/provisioning.py.
"""

import secrets

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """Custom user model with Auth0 integration and role support."""

    class Role(models.TextChoices):
        SUPERADMIN = "superadmin", "OrcaMet Admin"
        CLIENT_ADMIN = "client_admin", "Client Admin"
        CLIENT_USER = "client_user", "Client User"

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.CLIENT_USER,
    )

    # Link to Auth0 user ID (sub claim)
    auth0_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        unique=True,
        help_text="Auth0 user identifier (sub claim)",
    )

    # Link to client organisation
    client = models.ForeignKey(
        "sites.Client",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
        help_text="The client organisation this user belongs to",
    )

    class Meta:
        ordering = ["username"]

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def is_superadmin(self):
        return self.role == self.Role.SUPERADMIN

    @property
    def is_client_admin(self):
        return self.role == self.Role.CLIENT_ADMIN

    @property
    def is_client_user(self):
        return self.role == self.Role.CLIENT_USER

    @property
    def can_edit_thresholds(self):
        return self.role in (self.Role.SUPERADMIN, self.Role.CLIENT_ADMIN)

    @property
    def is_sandbox_user(self):
        """True for a self-provisioned test account (see Invite)."""
        return bool(self.client and self.client.is_sandbox)


def generate_invite_code():
    """A short, URL-safe, unguessable invite code."""
    return secrets.token_urlsafe(9)


class Invite(models.Model):
    """
    A shareable signup link that lets someone create their own test account.

    Without one, the Auth0 callback refuses anyone who has no Django user —
    accounts are created by OrcaMet staff only. An invite relaxes that for
    the holder of the link: on first login they get a User plus their own
    private sandbox Client to add sites to.

    Deliberately not a per-email invite: the point is to hand a friend a link
    without needing to know which address they will sign in with.
    """

    code = models.CharField(
        max_length=64,
        unique=True,
        default=generate_invite_code,
        help_text="The secret in the signup URL. Leave blank to generate one.",
    )
    label = models.CharField(
        max_length=200,
        blank=True,
        help_text="What this invite is for, e.g. 'Dave — rope access trial'",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Untick to revoke this invite immediately.",
    )
    max_uses = models.PositiveIntegerField(
        default=1,
        help_text="How many accounts this invite may create. 0 means unlimited.",
    )
    uses = models.PositiveIntegerField(default=0, editable=False)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Optional. After this moment the invite stops working.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invites_created",
        editable=False,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.label or self.code

    @property
    def is_exhausted(self):
        # max_uses == 0 is the documented "unlimited" sentinel.
        return self.max_uses != 0 and self.uses >= self.max_uses

    @property
    def is_expired(self):
        return self.expires_at is not None and timezone.now() >= self.expires_at

    @property
    def is_usable(self):
        return self.is_active and not self.is_expired and not self.is_exhausted

    def status(self):
        """Human-readable state, for the admin list."""
        if not self.is_active:
            return "Revoked"
        if self.is_expired:
            return "Expired"
        if self.is_exhausted:
            return "Used up"
        return "Usable"
