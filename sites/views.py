"""
OrcaMet Portal — Self-service site management.

These views let a workspace manage its own sites — add them one at a time, or
import a list in bulk — without OrcaMet staff creating rows in the Django
admin for them.

The capability is granted per client, by the invite that provisioned the
workspace (see accounts/provisioning.py). Clients created by staff in the
admin do not have it and are unaffected: their sites stay staff-managed, as
they always have been.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from .forms import SiteForm
from .importer import ImportPlan, parse_rows, parse_upload, plan_import, create_sites
from .models import ChangeLog, Site, ThresholdProfile
from .presets import DEFAULT_PRESET, PRESETS

logger = logging.getLogger(__name__)

# Where a previewed import waits for its confirmation.
IMPORT_SESSION_KEY = "site_import_plan"


def _self_service_client(user):
    """
    Return the Client this user may manage sites for, or raise PermissionDenied.

    Two conditions, both required: the workspace has been granted
    self-service, and this user is an admin of it rather than a read-only
    member.
    """
    client = user.client
    if client is None or not client.self_service_sites:
        raise PermissionDenied(
            "Sites for this account are managed by OrcaMet. "
            "Get in touch and we will add them for you."
        )
    if not user.can_edit_thresholds:
        raise PermissionDenied("Only an account admin can change sites.")
    return client


def _site_allowance(client):
    """(active site count, cap, whether another may be added)."""
    used = Site.objects.filter(client=client, is_active=True).count()
    cap = client.effective_site_limit
    return used, cap, used < cap


@login_required(login_url="/login/")
def site_create(request):
    """Add a single site to the logged-in user's own workspace."""
    client = _self_service_client(request.user)
    used, cap, may_add = _site_allowance(client)

    if not may_add:
        messages.error(
            request,
            f"Your account is limited to {cap} active sites. "
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
    """Edit one of this workspace's own sites."""
    client = _self_service_client(request.user)
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
    Remove one of this workspace's own sites, freeing a slot against the cap.

    Deactivates rather than deleting: the forecast history stays intact for
    cleanup_forecasts to age out normally, and ChangeLog rows survive.
    """
    client = _self_service_client(request.user)
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


# ============================================================
# BULK IMPORT
# ============================================================
#
# Two requests, deliberately. The first parses and geocodes and shows what
# would happen; the second creates it. Splitting them is what makes a
# thirty-row paste with two typos in it a five-second fix rather than a
# guessing game, and it keeps the (slow, networked) geocode off the path that
# writes to the database.


def _import_context(request, client, plan=None, text="", preset=""):
    """Shared template context for both halves of the import flow."""
    used, cap, _ = _site_allowance(client)
    return {
        "plan": plan,
        "text": text,
        "preset": preset or DEFAULT_PRESET,
        "presets": PRESETS,
        "sites_used": used,
        "sites_cap": cap,
        "sites_remaining": max(cap - used, 0),
        "client": client,
        # Onboarding renders this same view inside the wizard, where the
        # buttons lead onward instead of back to the dashboard.
        "in_onboarding": request.GET.get("onboarding") == "1",
    }


def _preview_template(request, context):
    """
    Which preview page to render.

    The import posts to this view from two places. From the wizard it has to
    come back wearing the wizard's chrome — losing the progress strip
    mid-setup reads as having been thrown out of the flow — so the onboarding
    page extends the wizard shell and needs its step context.
    """
    if not context["in_onboarding"]:
        return "sites/site_import_preview.html"

    # Imported here, not at module scope: onboarding.views imports from this
    # module, and a top-level import back would close the loop.
    from onboarding.steps import progress

    context["steps"] = progress("sites")
    context["current_slug"] = "sites"
    return "onboarding/sites_preview.html"


@login_required(login_url="/login/")
def site_import(request):
    """
    Paste or upload a list of sites, and preview what it would create.

    GET shows the empty form. POST parses, geocodes in one bulk call, and
    re-renders with a per-row verdict. Nothing is written here — the plan is
    parked in the session for site_import_confirm.
    """
    client = _self_service_client(request.user)

    if request.method != "POST":
        request.session.pop(IMPORT_SESSION_KEY, None)
        return render(request, "sites/site_import.html",
                      _import_context(request, client))

    text = request.POST.get("sites", "")
    preset = request.POST.get("preset", DEFAULT_PRESET)
    upload = request.FILES.get("file")

    if upload is not None:
        rows = parse_upload(upload)
        # Show the file's contents in the textarea so a rejected row can be
        # corrected in place rather than by editing the file and re-uploading.
        text = "\n".join(row.raw for row in rows if row.raw)
    else:
        rows = parse_rows(text)

    if not rows:
        messages.error(request, "There was nothing to import — add some sites first.")
        context = _import_context(request, client, text=text, preset=preset)
        if context["in_onboarding"]:
            return redirect("onboarding:sites")
        return render(request, "sites/site_import.html", context)

    plan = plan_import(rows, client, preset=preset)
    request.session[IMPORT_SESSION_KEY] = plan.to_session()

    context = _import_context(request, client, plan=plan, text=text, preset=preset)
    return render(request, _preview_template(request, context), context)


@login_required(login_url="/login/")
def site_import_confirm(request):
    """
    Create the sites from the previewed plan.

    POST only: this writes. The plan comes from the session rather than from
    the posted form, so the coordinates that get stored are the ones we
    looked up, not ones a form field asserted.
    """
    client = _self_service_client(request.user)

    if request.method != "POST":
        return redirect("sites:site_import")

    plan = ImportPlan.from_session(request.session.get(IMPORT_SESSION_KEY))
    if plan is None or not plan.ready:
        messages.error(
            request,
            "That import had expired — please paste your sites again.",
        )
        return redirect("sites:site_import")

    created, skipped = create_sites(plan, client, request.user)
    request.session.pop(IMPORT_SESSION_KEY, None)

    if created:
        messages.success(
            request,
            f"Added {len(created)} site"
            f"{'' if len(created) == 1 else 's'}. "
            # Honest about the queue: sites/signals.py runs at most
            # FORECAST_MAX_CONCURRENT_THREADS forecasts per worker and leaves
            # the rest to the scheduled job, so a big batch fills in
            # gradually. Promising "a minute or two" here would have people
            # reloading an empty dashboard.
            f"The first forecasts are generating now and the rest will fill "
            f"in over the next hour.",
        )
    if skipped:
        messages.warning(
            request,
            f"{len(skipped)} site{'' if len(skipped) == 1 else 's'} could not "
            f"be added: "
            + "; ".join(f"{row.name} — {row.error}" for row in skipped[:5])
            + ("…" if len(skipped) > 5 else ""),
        )

    if request.POST.get("onboarding") == "1":
        return redirect("onboarding:thresholds")
    return redirect("dashboard:home")
