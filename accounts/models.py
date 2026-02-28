"""
OrcaMet Portal — User Model

Three roles:
  - superadmin: OrcaMet staff (Steve). Full access to everything.
  - client_admin: Client company manager. Can edit thresholds for their sites.
  - client_user: Read-only access to their client's sites and forecasts.
"""

from django.contrib.auth.models import AbstractUser
from django.db import models


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
