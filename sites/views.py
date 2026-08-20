"""
OrcaMet Portal — Self-service site management.

These views exist for invite-provisioned trial accounts, so a tester can add
their own sites and see real forecasts without OrcaMet staff creating rows in
the Django admin for them. Real client sites are still managed by staff.
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from .forms import SiteForm
from .models import ChangeLog, Site, ThresholdProfile

logger = logging.getLogger(__name__)


def _sandbox_client(user):
    """
    Return the sandbox Client this user may manage, or raise PermissionDenied.

    Only sandbox owners get self-service site management. Everyone else —
    including real client admins — is unchanged: their sites are managed by
    OrcaMet staff through the Django admin.
    """
    if not user.is_sandbox_user:
        raise PermissionDenied("Self-service site management is for trial accounts.")
    return user.client


def _site_allowance(client):
    """(active site count, cap, whether another may be added)."""
    used = Site.objects.filter(client=client, is_active=True).count()
    cap = settings.SANDBOX_MAX_SITES
    return used, cap, used < cap


@login_required(login_url="/login/")
def site_create(request):
    """Add a site to the logged-in tester's own sandbox."""
    client = _sandbox_client(request.user)
    used, cap, may_add = _site_allowance(client)

    if not may_add:
        messages.error(
            request,
            f"Trial accounts are limited to {cap} active sites. "
            f"Remove one to add another.",
        )
        return redirect("dashboard:home")

    if request.method == "POST":
        form = SiteForm(request.POST, client=client)
        if form.is_valid():
            # One transaction so the forecast run — which the post_save signal
            # queues via transaction.on_commit — cannot start before the
            # threshold profile exists. Without this the site commits on its
            # own (ATOMIC_REQUESTS is off), the background thread starts
            # immediately, and the runner scores that first forecast against
            # its hardcoded fallback limits instead of the site's own.
            with transaction.atomic():
                site = form.save(commit=False)
                # Set from the session user, never from posted data.
                site.client = client
                site.save()

                # Every site needs an active threshold profile or the forecast
                # engine has no limits to score against. Values come from the
                # model field defaults.
                ThresholdProfile.objects.create(site=site, created_by=request.user)

                ChangeLog.objects.create(
                    site=site,
                    action=ChangeLog.Action.SITE_CREATED,
                    user=request.user,
                    details={"name": site.name, "postcode": site.postcode},
                )

            # The post_save signal has already queued a forecast run.
            messages.success(
                request,
                f"{site.name} added. Its first forecast is generating now and "
                f"should appear within a minute or two.",
            )
            return redirect("dashboard:site_detail", site_id=site.pk)
    else:
        form = SiteForm(client=client)

    return render(request, "sites/site_form.html", {
        "form": form,
        "site": None,
        "sites_used": used,
        "sites_cap": cap,
    })


@login_required(login_url="/login/")
def site_edit(request, site_id):
    """Edit one of the tester's own sites."""
    client = _sandbox_client(request.user)
    # Scoped to their client, so a guessed id from another workspace 404s.
    site = get_object_or_404(Site, pk=site_id, client=client, is_active=True)

    if request.method == "POST":
        form = SiteForm(request.POST, instance=site, client=client)
        if form.is_valid():
            site = form.save()
            ChangeLog.objects.create(
                site=site,
                action=ChangeLog.Action.SITE_UPDATED,
                user=request.user,
                details={"changed": sorted(form.changed_data)},
            )
            messages.success(request, f"{site.name} updated.")
            return redirect("dashboard:site_detail", site_id=site.pk)
    else:
        form = SiteForm(instance=site, client=client)

    used, cap, _ = _site_allowance(client)
    return render(request, "sites/site_form.html", {
        "form": form,
        "site": site,
        "sites_used": used,
        "sites_cap": cap,
    })


@login_required(login_url="/login/")
def site_delete(request, site_id):
    """
    Remove one of the tester's own sites, freeing a slot against the cap.

    Deactivates rather than deleting: the forecast history stays intact for
    cleanup_forecasts to age out normally, and ChangeLog rows survive.
    """
    client = _sandbox_client(request.user)
    site = get_object_or_404(Site, pk=site_id, client=client, is_active=True)

    if request.method != "POST":
        return render(request, "sites/site_confirm_delete.html", {"site": site})

    site.is_active = False
    site.save(update_fields=["is_active"])

    ChangeLog.objects.create(
        site=site,
        action=ChangeLog.Action.SITE_DEACTIVATED,
        user=request.user,
        details={"name": site.name},
    )

    messages.success(request, f"{site.name} removed.")
    return redirect("dashboard:home")
