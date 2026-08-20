"""
OrcaMet Portal — Invite-based account provisioning.

Normally the Auth0 callback refuses anyone without a matching Django user:
accounts are created by OrcaMet staff in the admin. This module is the one
exception — someone holding a valid invite link gets an account created for
them on first login, together with their own private sandbox Client.

Kept out of views.py so the trust rules live in one auditable place.
"""

import logging

from django.db import transaction
from django.db.models import F
from django.utils.text import slugify

from sites.models import Client

from .models import Invite, User

logger = logging.getLogger(__name__)

# Where the invite code is parked between /signup/ and the Auth0 callback.
SESSION_KEY = "signup_invite_code"


def lookup_invite(code):
    """
    Return the Invite for this code if it exists and may still be used,
    otherwise None. Never raises on a malformed or absent code.
    """
    if not code or not isinstance(code, str):
        return None
    invite = Invite.objects.filter(code=code.strip()).first()
    if invite is None or not invite.is_usable:
        return None
    return invite


def _unique_username(email):
    """
    Derive a stable, unique username from an email address.

    Django still requires a username even though we authenticate via Auth0.
    """
    base = slugify(email.split("@")[0]) or "user"
    base = base[:140]
    candidate = base
    suffix = 2
    while User.objects.filter(username__iexact=candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _sandbox_client_name(name, email):
    """A recognisable, unique name for the tester's own workspace."""
    label = (name or email.split("@")[0]).strip()
    base = f"{label} (Sandbox)"[:200]
    candidate = base
    suffix = 2
    while Client.objects.filter(name__iexact=candidate).exists():
        candidate = f"{base[:194]} #{suffix}"
        suffix += 1
    return candidate


@transaction.atomic
def provision_sandbox_user(invite, auth0_id, email, name):
    """
    Create a test account and its private sandbox Client for `invite`.

    The caller is responsible for having verified the identity — in
    particular that Auth0 asserted the email address is verified. Returns the
    new User, or None if the invite was consumed by a concurrent signup.

    Atomic so a failure part-way cannot leave an orphaned Client or a user
    with no workspace, and so the usage count cannot drift from the number of
    accounts actually created.
    """
    # Re-check under a row lock. is_usable was evaluated before the round trip
    # to Auth0, which can be minutes earlier — the invite may have been
    # revoked or used up by someone else in the meantime.
    locked = Invite.objects.select_for_update().filter(pk=invite.pk).first()
    if locked is None or not locked.is_usable:
        logger.warning(
            "Invite %s no longer usable at provisioning time (sub=%s)",
            invite.pk, auth0_id,
        )
        return None

    client = Client.objects.create(
        name=_sandbox_client_name(name, email),
        contact_name=name or "",
        contact_email=email or "",
        is_sandbox=True,
        notes=f"Trial workspace created from invite '{locked}'.",
    )

    first_name, _, last_name = (name or "").partition(" ")

    user = User.objects.create_user(
        username=_unique_username(email),
        email=email,
        first_name=first_name,
        last_name=last_name,
        # password=None makes create_user set an unusable password, so this
        # account can only ever be reached through Auth0 and there is nothing
        # to brute-force at the Django end.
        password=None,
        auth0_id=auth0_id or None,
        # Admin of their own sandbox only, which is what lets them add sites
        # and edit thresholds there. Scoping is enforced per-view against
        # user.client, so this grants nothing outside their own workspace.
        role=User.Role.CLIENT_ADMIN,
        client=client,
    )

    # F() so concurrent signups on a multi-use invite cannot lose a count.
    Invite.objects.filter(pk=locked.pk).update(uses=F("uses") + 1)

    logger.info(
        "Provisioned sandbox account %s (client=%s) from invite %s",
        user.username, client.name, locked.pk,
    )
    return user
