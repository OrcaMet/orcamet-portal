"""
OrcaMet Portal — Onboarding wizard.

Five steps between an invite link and a working dashboard. The client tells
us who they are, picks the limits they work to, imports their sites, checks
the numbers, and is done.

Two rules shape the whole thing:

  * It is resumable. Every step can be reached directly and progress is a
    database fact (Client.onboarding_completed_at, plus whether any sites
    exist), so closing the laptop half way through costs nothing.

  * It never blocks the dashboard permanently. Steps can be skipped, and
    "Finish" is always available from the last step, because a client who
    just wants to look around should be able to.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.utils import timezone

from sites.models import Site, ThresholdProfile
from sites.presets import DEFAULT_PRESET, PRESETS, thresholds_for
from sites.views import _import_context, _self_service_client

from .forms import OperationForm, ThresholdsForm
from .steps import progress

logger = logging.getLogger(__name__)

# The preset chosen in step 2, held until the sites it applies to exist.
PRESET_SESSION_KEY = "onboarding_preset"

# Plain-language gloss for the threshold fields, so step 4 is a decision
# somebody can actually make rather than ten unlabelled numbers. Keyed by
# field name; the unit comes from the model.
THRESHOLD_NOTES = {
    "wind_mean_caution": "Steady wind we start flagging at. 9 m/s is about 17 knots — a fresh breeze.",
    "wind_mean_cancel": "Steady wind we call a stop at.",
    "gust_caution": "Gusts we start flagging at. Usually the limit that bites first at height.",
    "gust_cancel": "Gusts we call a stop at.",
    "precip_caution": "Rain rate we start flagging at. 0.5 mm/h is light but persistent.",
    "precip_cancel": "Rain rate we call a stop at.",
    "temp_min_caution": "Cold we start flagging at — dexterity and ice on steelwork.",
    "temp_min_cancel": "Cold we call a stop at.",
    "temp_max_caution": "Heat we start flagging at. Clear both heat boxes to ignore heat entirely.",
    "temp_max_cancel": "Heat we call a stop at.",
}


def _wizard_client(user):
    """
    The client whose onboarding this is, or PermissionDenied.

    Reuses the same gate as self-service site management: the wizard's third
    step *is* the bulk importer, so anyone who cannot import sites has
    nothing to do here. A read-only client_user is sent away too — they
    should not be setting the limits their colleagues work to.
    """
    return _self_service_client(user)


def _base_context(client, slug, **extra):
    context = {
        "client": client,
        "steps": progress(slug),
        "current_slug": slug,
    }
    context.update(extra)
    return context


@login_required(login_url="/login/")
def start(request):
    """
    Entry point. Sends the user to the first thing they have not done.

    Keeps the redirect logic in one place, so dashboard:home and any "resume
    setup" link do not each have to work out where somebody got to.
    """
    client = _wizard_client(request.user)

    if client.onboarding_complete:
        return redirect("dashboard:home")
    if not client.contact_email:
        return redirect("onboarding:welcome")
    if not Site.objects.filter(client=client, is_active=True).exists():
        return redirect("onboarding:sites")
    return redirect("onboarding:thresholds")


@login_required(login_url="/login/")
def welcome(request):
    """Step 1 — what this is and how to read it. Nothing to fill in."""
    client = _wizard_client(request.user)
    return render(request, "onboarding/welcome.html",
                  _base_context(client, "welcome"))


@login_required(login_url="/login/")
def operation(request):
    """Step 2 — contact details, and the limits to start from."""
    client = _wizard_client(request.user)

    if request.method == "POST":
        form = OperationForm(request.POST, instance=client)
        if form.is_valid():
            form.save()
            request.session[PRESET_SESSION_KEY] = form.cleaned_data["threshold_preset"]
            request.session["onboarding_exposure"] = form.cleaned_data["default_exposure"]
            return redirect("onboarding:sites")
    else:
        form = OperationForm(instance=client, initial={
            "threshold_preset": request.session.get(PRESET_SESSION_KEY, DEFAULT_PRESET),
        })

    return render(request, "onboarding/operation.html", _base_context(
        client, "operation", form=form, presets=PRESETS,
    ))


@login_required(login_url="/login/")
def sites(request):
    """
    Step 3 — import the client's locations.

    Renders the bulk importer's own form rather than a wizard copy of it, so
    there is one import UI to maintain and the two cannot drift. The template
    posts to sites:site_import with onboarding=1, which is what sends the
    user on to step 4 after confirming instead of back to the dashboard.
    """
    client = _wizard_client(request.user)

    context = _base_context(client, "sites")
    context.update(_import_context(
        request, client,
        preset=request.session.get(PRESET_SESSION_KEY, DEFAULT_PRESET),
    ))
    context["in_onboarding"] = True
    context["existing_sites"] = Site.objects.filter(client=client, is_active=True)

    return render(request, "onboarding/sites.html", context)


@login_required(login_url="/login/")
def thresholds(request):
    """
    Step 4 — check the limits before they start flagging real jobs.

    Edits the profile of the client's first site and, by default, copies the
    result to every other site. Copying is the right default here: everything
    in this workspace was just created from one preset seconds ago, so the
    user is reviewing a single set of numbers, not ten different ones. Per
    site divergence comes later, from the site page.
    """
    client = _wizard_client(request.user)

    first_site = Site.objects.filter(client=client, is_active=True).first()
    if first_site is None:
        messages.info(request, "Add a site first, then we can set your limits.")
        return redirect("onboarding:sites")

    profile = ThresholdProfile.objects.filter(
        site=first_site, is_active=True
    ).first()
    if profile is None:
        # Belt and braces: every site should get one at creation, but a
        # profile-less site would otherwise be scored against the engine's
        # fallbacks with nothing on screen to say so.
        preset = request.session.get(PRESET_SESSION_KEY, DEFAULT_PRESET)
        profile = ThresholdProfile.objects.create(
            site=first_site, created_by=request.user, **thresholds_for(preset)
        )

    if request.method == "POST":
        form = ThresholdsForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()

            if form.cleaned_data["apply_to_all"]:
                count = _copy_thresholds(client, profile, request.user)
                messages.success(
                    request,
                    f"Limits saved and applied to {count} site"
                    f"{'' if count == 1 else 's'}.",
                )
            else:
                messages.success(request, f"Limits saved for {first_site.name}.")

            return redirect("onboarding:done")
    else:
        form = ThresholdsForm(instance=profile)

    return render(request, "onboarding/thresholds.html", _base_context(
        client, "thresholds",
        form=form,
        site=first_site,
        site_count=Site.objects.filter(client=client, is_active=True).count(),
        # Paired up here rather than looked up in the template: Django
        # templates cannot index a dict by a variable key, and the field
        # order is the order the form declares, which is the order these
        # should be read in.
        fields_with_notes=[
            (form[name], THRESHOLD_NOTES[name]) for name in THRESHOLD_NOTES
        ],
    ))


def _copy_thresholds(client, source, user):
    """
    Give every active site of `client` the values in `source`.

    Follows the pattern ThresholdProfile documents for itself — deactivate
    the old profile and create a new one rather than editing in place — so
    the history of what a forecast was scored against is not rewritten
    retrospectively.
    """
    from sites.models import ChangeLog

    values = source.as_dict()
    count = 0

    for site in Site.objects.filter(client=client, is_active=True):
        if site.pk == source.site_id:
            count += 1
            continue

        ThresholdProfile.objects.filter(site=site, is_active=True).update(
            is_active=False
        )
        ThresholdProfile.objects.create(site=site, created_by=user, **values)
        ChangeLog.objects.create(
            site=site,
            action=ChangeLog.Action.THRESHOLD_UPDATED,
            user=user,
            details={"source": "onboarding", "applied_from": source.site.name},
        )
        count += 1

    return count


@login_required(login_url="/login/")
def done(request):
    """
    Step 5 — stamp completion and hand over to the dashboard.

    GET shows the summary; POST is what actually completes, so a crawler or
    a prefetch cannot mark somebody's setup finished behind their back.
    """
    client = _wizard_client(request.user)

    if request.method == "POST":
        if not client.onboarding_complete:
            client.onboarding_completed_at = timezone.now()
            client.save(update_fields=["onboarding_completed_at"])
            logger.info("Onboarding completed for client %s", client.name)

        request.session.pop(PRESET_SESSION_KEY, None)
        request.session.pop("onboarding_exposure", None)
        messages.success(request, "You are all set. Here is your dashboard.")
        return redirect("dashboard:home")

    site_count = Site.objects.filter(client=client, is_active=True).count()
    return render(request, "onboarding/done.html", _base_context(
        client, "done", site_count=site_count,
    ))


@login_required(login_url="/login/")
def skip(request):
    """
    Leave the wizard without finishing it.

    Marks onboarding complete so the dashboard stops redirecting here, which
    is the point — somebody who wants to look around first should not be
    herded back every time they click Dashboard. The wizard stays reachable
    at its own URLs, and clearing onboarding_completed_at in the admin brings
    the redirect back.
    """
    client = _wizard_client(request.user)

    if request.method != "POST":
        raise PermissionDenied("Use the Skip button.")

    if not client.onboarding_complete:
        client.onboarding_completed_at = timezone.now()
        client.save(update_fields=["onboarding_completed_at"])

    messages.info(
        request,
        "Setup skipped. You can add sites any time from your dashboard.",
    )
    return redirect("dashboard:home")
