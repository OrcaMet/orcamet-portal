"""
OrcaMet Portal — Auth0 Authentication Views
"""

import logging
from authlib.integrations.django_client import OAuth
from django.conf import settings
from django.shortcuts import redirect, render
from django.urls import reverse
from django.contrib.auth import login as django_login, logout as django_logout
from urllib.parse import quote_plus, urlencode

from .models import User
from .provisioning import SESSION_KEY, lookup_invite, provision_sandbox_user

logger = logging.getLogger(__name__)

# ============================================================
# AUTH0 OAUTH CLIENT
# ============================================================

oauth = OAuth()

oauth.register(
    "auth0",
    client_id=settings.AUTH0_CLIENT_ID,
    client_secret=settings.AUTH0_CLIENT_SECRET,
    client_kwargs={
        "scope": "openid profile email",
    },
    server_metadata_url=f"https://{settings.AUTH0_DOMAIN}/.well-known/openid-configuration",
)


# ============================================================
# VIEWS
# ============================================================

def index(request):
    """Landing page — shows login or redirects to dashboard."""
    if request.user.is_authenticated:
        return redirect("dashboard:home")
    return render(request, "accounts/index.html")


def login_view(request):
    """Redirect to Auth0 for authentication."""
    # DO NOT flush session here — Authlib stores the OAuth state
    # in the session and needs it when the callback arrives.
    return oauth.auth0.authorize_redirect(
        request,
        request.build_absolute_uri(reverse("accounts:callback")),
    )


def signup_view(request):
    """
    Entry point for an invite link: /signup/?invite=<code>

    Validates the code, parks it in the session, and hands off to Auth0. The
    session is where the code has to live — Auth0 sends the user back to a
    fixed callback URL, so it cannot ride along in the query string.
    """
    if request.user.is_authenticated:
        return redirect("dashboard:home")

    code = request.GET.get("invite", "")
    invite = lookup_invite(code)

    if invite is None:
        # Deliberately does not distinguish "no such code" from "revoked" or
        # "used up": that difference is only useful to someone guessing codes.
        logger.info("Signup attempted with an invalid invite code")
        return render(request, "accounts/invite_invalid.html", status=404)

    request.session[SESSION_KEY] = invite.code

    return oauth.auth0.authorize_redirect(
        request,
        request.build_absolute_uri(reverse("accounts:callback")),
        # Land on Auth0's sign-up tab rather than its login tab — the whole
        # point of this link is that the person has no account yet.
        screen_hint="signup",
    )


def callback_view(request):
    """
    Auth0 callback — exchanges the authorisation code for tokens,
    finds the matching Django user, and logs them in.
    """
    try:
        token = oauth.auth0.authorize_access_token(request)
    except Exception as e:
        logger.error(f"Auth0 token exchange failed: {e}", exc_info=True)
        return render(request, "accounts/login_error.html", {
            "error": f"Authentication failed: {e}",
        })

    userinfo = token.get("userinfo", {})

    auth0_id = str(userinfo.get("sub", ""))
    email = str(userinfo.get("email", ""))
    name = str(userinfo.get("name", ""))
    email_verified = userinfo.get("email_verified") is True

    logger.info(f"Auth0 callback received: sub={auth0_id}, verified={email_verified}")

    # Store only simple strings in session
    request.session["auth0_user"] = {
        "sub": auth0_id,
        "email": email,
        "name": name,
    }

    # Find existing user by Auth0 ID, or by email as fallback
    user = None
    if auth0_id:
        user = User.objects.filter(auth0_id=auth0_id).first()

    # Fall back to matching on email — but only if Auth0 asserts the address
    # has been verified. Without that check, anyone who can register at the
    # identity provider with an existing user's address would be linked
    # straight into that account, including a superadmin one.
    if user is None and email and email_verified:
        user = User.objects.filter(email__iexact=email).first()
        if user and not user.auth0_id:
            user.auth0_id = auth0_id
            user.save(update_fields=["auth0_id"])

    if user is None and email and not email_verified:
        logger.warning(
            f"Refusing email-based account link for unverified address (sub={auth0_id})"
        )

    # Nobody matched. If this login started from a valid invite link, create
    # the account now; otherwise access is refused, as before.
    if user is None:
        invite = lookup_invite(request.session.get(SESSION_KEY))

        if invite is not None and email and email_verified:
            user = provision_sandbox_user(invite, auth0_id, email, name)
            request.session.pop(SESSION_KEY, None)
            if user is None:
                # The invite was revoked or used up between the click and here.
                return render(request, "accounts/invite_invalid.html", status=410)

        elif invite is not None and not email_verified:
            # Provisioning on an unverified address would let someone claim an
            # invite under any address they like, including one belonging to a
            # real client — which the email-matching branch above would then
            # honour on a later login.
            logger.warning(
                f"Refusing invite signup for unverified address (sub={auth0_id})"
            )
            # Deliberately keep the code in the session. Auth0 sends its
            # verification email but returns the user here immediately, so
            # this branch is the normal path, not an edge case. Holding the
            # code means that once they verify, an ordinary Login completes
            # the signup — otherwise they would have to find the original
            # invite link again, which most people will have lost by then.
            # No weaker: email_verified is re-checked on that later request.
            return render(request, "accounts/verify_email.html", {
                "email": email,
            }, status=403)

    if user is None:
        logger.warning(f"No Django user found for sub={auth0_id}")
        return render(request, "accounts/no_access.html", {
            "email": email,
            "name": name,
        })

    # Consume any leftover code so a stale invite cannot be reused later.
    request.session.pop(SESSION_KEY, None)

    # Update name from Auth0 if we don't have it yet
    updated_fields = []
    if name and not user.first_name:
        parts = name.split(" ", 1)
        user.first_name = parts[0]
        if len(parts) > 1:
            user.last_name = parts[1]
        updated_fields.extend(["first_name", "last_name"])
    if updated_fields:
        user.save(update_fields=updated_fields)

    # Log into Django session
    django_login(request, user)
    logger.info(f"User logged in: {user.username} ({user.role})")

    return redirect("dashboard:home")


def logout_view(request):
    """Clear Django session and redirect to Auth0 logout."""
    django_logout(request)
    request.session.flush()

    return redirect(
        f"https://{settings.AUTH0_DOMAIN}/v2/logout?"
        + urlencode(
            {
                "returnTo": request.build_absolute_uri(reverse("accounts:index")),
                "client_id": settings.AUTH0_CLIENT_ID,
            },
            quote_via=quote_plus,
        )
    )
