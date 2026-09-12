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

    An invite decides three things about the workspace the holder lands in:
    whether it is a trial or a real client (`creates_sandbox`), whether it is
    new or one that already exists (`existing_client`), and what the holder
    may do in it (`granted_role`).
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

    # ---- What kind of workspace this invite leads to ----

    creates_sandbox = models.BooleanField(
        default=True,
        help_text=(
            "On: a trial workspace, named '<their name> (Sandbox)'. "
            "Off: a real client workspace — use this for a paying client, "
            "and set the client name below."
        ),
    )
    client_name = models.CharField(
        max_length=200,
        blank=True,
        help_text=(
            "Name for the workspace this invite creates, e.g. 'Summit Rope "
            "Access Ltd'. Leave blank to derive one from whoever signs up. "
            "Ignored when joining an existing client."
        ),
    )
    existing_client = models.ForeignKey(
        "sites.Client",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="invites",
        help_text=(
            "Join this existing client instead of creating a workspace. "
            "This is how a client brings colleagues in — set max uses to the "
            "size of the team, or 0 for unlimited."
        ),
    )
    site_limit = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Active site cap for the workspace this invite creates. "
            "0 falls back to SANDBOX_MAX_SITES. Ignored when joining an "
            "existing client, whose own limit applies."
        ),
    )
    granted_role = models.CharField(
        max_length=20,
        choices=[
            (User.Role.CLIENT_ADMIN, User.Role.CLIENT_ADMIN.label),
            (User.Role.CLIENT_USER, User.Role.CLIENT_USER.label),
        ],
        default=User.Role.CLIENT_ADMIN,
        help_text=(
            "Client Admin can manage sites and thresholds. Client User is "
            "read-only — the right choice for a colleague joining an "
            "existing client. Superadmin is deliberately not offered."
        ),
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

    def clean(self):
        """
        Reject combinations that would quietly do something other than what
        the wording of the fields promises.
        """
        from django.core.exceptions import ValidationError

        errors = {}

        if self.existing_client_id:
            # Joining a workspace creates nothing, so every field describing
            # what to create is dead weight — and reads as if it applies.
            if self.creates_sandbox:
                errors["creates_sandbox"] = (
                    "Untick this when joining an existing client — the "
                    "workspace already exists and its own settings apply."
                )
            if self.client_name:
                errors["client_name"] = (
                    "Leave blank when joining an existing client."
                )
            if self.site_limit:
                errors["site_limit"] = (
                    "Leave at 0 when joining an existing client — the "
                    "client's own site limit applies."
                )
        elif not self.creates_sandbox and not self.client_name:
            errors["client_name"] = (
                "Name the client this invite is for. Only trial workspaces "
                "get a name derived from whoever signs up."
            )

        if errors:
            raise ValidationError(errors)

    def signup_path(self):
        """The path to hand out. Combine with the site's domain to share it."""
        return f"/signup/?invite={self.code}"

    def status(self):
        """Human-readable state, for the admin list."""
        if not self.is_active:
            return "Revoked"
        if self.is_expired:
            return "Expired"
        if self.is_exhausted:
            return "Used up"
        return "Usable"
