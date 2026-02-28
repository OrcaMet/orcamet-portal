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

    logger.info(f"Auth0 callback received: email={email}, sub={auth0_id}")

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

    if user is None and email:
        user = User.objects.filter(email__iexact=email).first()
        if user and not user.auth0_id:
            user.auth0_id = auth0_id
            user.save(update_fields=["auth0_id"])

    if user is None:
        logger.warning(f"No Django user found for email={email}")
        return render(request, "accounts/no_access.html", {
            "email": email,
            "name": name,
        })

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
