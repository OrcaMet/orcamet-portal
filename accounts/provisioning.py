"""
OrcaMet Portal — Invite-based account provisioning.

Normally the Auth0 callback refuses anyone without a matching Django user:
accounts are created by OrcaMet staff in the admin. This module is the one
exception — someone holding a valid invite link gets an account created for
them on first login, in the workspace the invite describes.

An invite leads to one of three places:

  * a trial workspace  — `creates_sandbox`, capped, labelled "(Sandbox)"
  * a real client      — a paying client, named on the invite
  * an existing client — a colleague joining a workspace already set up

The first two create a Client and grant it self-service site management. The
third creates nothing and inherits whatever that client already has.

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


def _unique_client_name(base):
    """Make `base` unique among Clients, since nothing else disambiguates."""
    base = base[:200]
    candidate = base
    suffix = 2
    while Client.objects.filter(name__iexact=candidate).exists():
        candidate = f"{base[:194]} #{suffix}"
        suffix += 1
    return candidate


def _workspace_name(invite, name, email):
    """
    What to call the workspace this invite creates.

    A real client is named on the invite, because the company's name is known
    before anyone signs up. A trial is not, so it is derived from whoever
    turns up and marked as a sandbox so it cannot be mistaken for a client.
    """
    if not invite.creates_sandbox:
        return _unique_client_name(invite.client_name.strip())

    label = (invite.client_name or name or email.split("@")[0]).strip()
    return _unique_client_name(f"{label} (Sandbox)")


@transaction.atomic
def provision_user_from_invite(invite, auth0_id, email, name):
    """
    Create an account for `invite`, and its workspace if the invite makes one.

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
    locked = (
        Invite.objects.select_for_update()
        .select_related("existing_client")
        .filter(pk=invite.pk)
        .first()
    )
    if locked is None or not locked.is_usable:
        logger.warning(
            "Invite %s no longer usable at provisioning time (sub=%s)",
            invite.pk, auth0_id,
        )
        return None

    if locked.existing_client_id:
        # Joining, not creating. Nothing about the target workspace changes —
        # in particular its site limit is not raised by a new member, and a
        # staff-managed client does not become self-service because somebody
        # was invited into it.
        client = locked.existing_client
        created_workspace = False
    else:
        client = Client.objects.create(
            name=_workspace_name(locked, name, email),
            contact_name=name or "",
            contact_email=email or "",
            is_sandbox=locked.creates_sandbox,
            # The invite is the deliberate grant: a workspace nobody at
            # OrcaMet has set up has to be able to set itself up. Clients
            # created in the admin are unaffected and stay staff-managed.
            self_service_sites=True,
            site_limit=locked.site_limit,
            notes=f"Workspace created from invite '{locked}'.",
        )
        created_workspace = True

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
        # Scoping is enforced per-view against user.client, so even
        # client_admin grants nothing outside this one workspace.
        role=locked.granted_role,
        client=client,
    )

    # F() so concurrent signups on a multi-use invite cannot lose a count.
    Invite.objects.filter(pk=locked.pk).update(uses=F("uses") + 1)

    logger.info(
        "Provisioned account %s (client=%s, new_workspace=%s, role=%s) "
        "from invite %s",
        user.username, client.name, created_workspace, user.role, locked.pk,
    )
    return user
