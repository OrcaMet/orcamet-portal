"""
OrcaMet Portal — Bulk site import.

Turns a pasted list or an uploaded CSV into Site rows. A client arriving with
thirty work locations cannot reasonably add them one at a time, and the
realistic input is a paste out of a spreadsheet rather than a tidy CSV — so
the parser is deliberately forgiving about separators and column count, and
strict about telling you exactly which row it could not use.

Nothing here touches the request or the session. The rules are pure functions
over text so they can be tested without a logged-in client, and so the
"what would this do" preview and the "do it" step run identical logic.

Flow:

    parse_rows(text)   -> [ParsedRow]      what the user typed
    plan_import(...)   -> ImportPlan       what we would create, and why not
    create_sites(...)  -> [Site]           do it

plan_import does the network call (one bulk geocode), so the preview a user
confirms is the same object that gets created — confirming does not re-look
anything up.
"""

import csv
import io
import logging
from dataclasses import dataclass, field

from django.db import transaction

from .models import (
    ChangeLog, Site, ThresholdProfile,
    geocode_postcodes_bulk, normalise_postcode,
)
from .presets import thresholds_for

logger = logging.getLogger(__name__)

# A paste of a whole spreadsheet column is plausible; a paste of a novel is
# not. The cap keeps one request's geocoding and row rendering bounded.
MAX_ROWS = 250

# Column headers we recognise, so a CSV exported with a header row does not
# turn its header into a site called "Name".
_HEADER_WORDS = {"name", "site", "site name", "postcode", "post code",
                 "exposure", "elevation", "location"}

_EXPOSURE_BY_LABEL = {
    label.lower(): value for value, label in Site.Exposure.choices
}


@dataclass
class ParsedRow:
    """One line of user input, before we know whether it can be created."""

    line_number: int
    raw: str
    name: str = ""
    postcode: str = ""
    exposure: str = ""
    elevation: int = 0
    error: str = ""

    @property
    def ok(self):
        return not self.error


@dataclass
class PlannedSite:
    """A row that survived validation, with its resolved coordinates."""

    row: ParsedRow
    latitude: float
    longitude: float


@dataclass
class ImportPlan:
    """What checking a batch produced: what we would create, and what we would not."""

    ready: list = field(default_factory=list)     # [PlannedSite]
    rejected: list = field(default_factory=list)  # [ParsedRow], each with .error
    preset: str = ""

    @property
    def ready_count(self):
        return len(self.ready)

    @property
    def rejected_count(self):
        return len(self.rejected)

    @property
    def has_anything(self):
        return bool(self.ready or self.rejected)

    # -- Session round trip --
    #
    # The preview is shown on one request and confirmed on the next, and the
    # confirmed batch must be the one that was shown: re-parsing the textarea
    # on confirm would re-run the geocode, and carrying the coordinates in
    # hidden form fields would let a posted latitude decide where a site is.
    # So the plan is parked in the session, which the user cannot edit.
    #
    # Django serialises sessions as JSON, hence plain dicts rather than the
    # dataclasses themselves.

    def to_session(self):
        return {
            "preset": self.preset,
            "ready": [
                {
                    "line_number": planned.row.line_number,
                    "raw": planned.row.raw,
                    "name": planned.row.name,
                    "postcode": planned.row.postcode,
                    "exposure": planned.row.exposure,
                    "elevation": planned.row.elevation,
                    "latitude": planned.latitude,
                    "longitude": planned.longitude,
                }
                for planned in self.ready
            ],
        }

    @classmethod
    def from_session(cls, data):
        """
        Rebuild a plan parked by `to_session`, or None if it is unusable.

        Rejected rows are not carried across: they were feedback for the
        preview and nothing is created from them.
        """
        if not isinstance(data, dict):
            return None

        plan = cls(preset=data.get("preset") or "")
        for entry in data.get("ready") or []:
            try:
                plan.ready.append(PlannedSite(
                    row=ParsedRow(
                        line_number=int(entry["line_number"]),
                        raw=str(entry.get("raw", "")),
                        name=str(entry["name"]),
                        postcode=str(entry["postcode"]),
                        exposure=str(entry.get("exposure", "")),
                        elevation=int(entry.get("elevation") or 0),
                    ),
                    latitude=float(entry["latitude"]),
                    longitude=float(entry["longitude"]),
                ))
            except (KeyError, TypeError, ValueError):
                # A session written by an older release, or a truncated one.
                # Re-previewing costs the user one click; creating half a
                # batch would cost them an explanation.
                return None

        return plan


# ============================================================
# PARSING
# ============================================================

def _split_line(line):
    """
    Split one line into fields on whatever separator it actually uses.

    Tab first, because a paste out of Excel is tab-separated and a site name
    like "Tower Block A, rear elevation" would otherwise split on its own
    comma. Only then comma, then runs of two or more spaces — never a single
    space, which lives inside both site names and postcodes.
    """
    if "\t" in line:
        return [part.strip() for part in line.split("\t")]

    if "," in line:
        # csv rather than str.split so a quoted "Name, with comma" survives.
        reader = csv.reader(io.StringIO(line))
        try:
            return [part.strip() for part in next(reader)]
        except (StopIteration, csv.Error):
            return [line.strip()]

    parts = [part for part in line.split("  ") if part.strip()]
    if len(parts) > 1:
        return [part.strip() for part in parts]

    return [line.strip()]


def _looks_like_postcode(value):
    """
    A loose UK postcode shape: starts with a letter, contains a digit.

    Deliberately loose — postcodes.io is the authority on whether a postcode
    exists. All this decides is which field of an ambiguous row is the
    postcode, so over-matching costs a clear "postcode not found" and
    under-matching costs a confusing "no postcode on this line".
    """
    clean = normalise_postcode(value)
    if not 5 <= len(clean) <= 8:
        return False
    if not clean[0].isalpha():
        return False
    return any(char.isdigit() for char in clean)


def _parse_exposure(value):
    """Map typed text to a Site.Exposure value, or "" if unrecognised."""
    clean = (value or "").strip().lower()
    if not clean:
        return ""
    if clean in _EXPOSURE_BY_LABEL:
        return _EXPOSURE_BY_LABEL[clean]
    if clean in Site.Exposure.values:
        return clean
    return ""


def _parse_elevation(value):
    """Metres as an int, or None if it is not a number."""
    clean = (value or "").strip().rstrip("mM").strip()
    if not clean:
        return 0
    try:
        return int(round(float(clean)))
    except ValueError:
        return None


def parse_rows(text):
    """
    Parse pasted text into ParsedRows. Never raises on bad input.

    Accepts, per line:

        Name, Postcode
        Name, Postcode, Exposure
        Name, Postcode, Exposure, Elevation
        Postcode                      (the name defaults to the postcode)

    separated by tabs, commas, or runs of spaces. Blank lines are skipped
    silently and a header row is skipped. Lines that cannot be understood
    come back with `.error` set rather than being dropped, so the user can
    see what happened to the line they typed.
    """
    rows = []

    for index, line in enumerate((text or "").splitlines(), start=1):
        if not line.strip():
            continue

        if len(rows) >= MAX_ROWS:
            rows.append(ParsedRow(
                line_number=index, raw=line.strip(),
                error=(f"Not checked — imports are limited to {MAX_ROWS} "
                       f"rows at a time."),
            ))
            continue

        parts = [part for part in _split_line(line) if part != ""]
        if not parts:
            continue

        # A header row: every field is a column name we recognise.
        if not rows and all(part.lower() in _HEADER_WORDS for part in parts):
            continue

        row = ParsedRow(line_number=index, raw=line.strip())

        if len(parts) == 1:
            # A single field has to be a postcode, and names itself.
            if not _looks_like_postcode(parts[0]):
                row.error = (
                    "Couldn't read this line. Expected a postcode, or a name "
                    "and a postcode separated by a comma."
                )
                rows.append(row)
                continue
            row.postcode = parts[0]
            row.name = normalise_postcode(parts[0])
            rows.append(row)
            continue

        # Two or more fields. Normally "Name, Postcode, ...", but a
        # spreadsheet with the columns the other way round is common enough
        # to detect rather than reject.
        if _looks_like_postcode(parts[1]):
            row.name, row.postcode = parts[0], parts[1]
            rest = parts[2:]
        elif _looks_like_postcode(parts[0]):
            row.postcode, row.name = parts[0], parts[1]
            rest = parts[2:]
        else:
            row.error = (
                "No UK postcode found on this line. Expected something like "
                "'Tower Block A, EH1 1YZ'."
            )
            rows.append(row)
            continue

        if rest:
            row.exposure = _parse_exposure(rest[0])
            if not row.exposure:
                labels = ", ".join(label for _, label in Site.Exposure.choices)
                row.error = f"'{rest[0]}' is not an exposure. Use one of: {labels}."
                rows.append(row)
                continue

        if len(rest) > 1:
            elevation = _parse_elevation(rest[1])
            if elevation is None:
                row.error = f"'{rest[1]}' is not a number of metres."
                rows.append(row)
                continue
            row.elevation = elevation

        row.name = row.name.strip()[:200]
        if not row.name:
            row.name = normalise_postcode(row.postcode)

        rows.append(row)

    return rows


def parse_upload(uploaded_file):
    """
    Read an uploaded CSV or text file and parse it with `parse_rows`.

    One code path with the paste box on purpose: a file, and a paste of that
    same file's contents, must behave identically — otherwise one of the two
    quietly grows rules of its own.
    """
    data = uploaded_file.read()

    if isinstance(data, bytes):
        # utf-8-sig strips the byte-order mark Excel writes, which would
        # otherwise become part of the first site's name.
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                data = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return [ParsedRow(
                line_number=0, raw="",
                error="Couldn't read that file — save it as CSV and try again.",
            )]

    return parse_rows(data)


# ============================================================
# PLANNING
# ============================================================

def plan_import(rows, client, preset=""):
    """
    Decide which rows can become sites for `client`, and why the rest cannot.

    `preset` falls back to the client's own choice from onboarding, so a
    caller that does not care about presets still creates sites with the
    right limits.

    Does the geocoding, in one bulk call for the whole batch. The returned
    plan carries the resolved coordinates, so `create_sites` makes no network
    call and the user creates exactly what the preview showed them.

    Rejects, each reported against the line that caused it:
      * anything parse_rows already rejected
      * a postcode postcodes.io does not recognise
      * a name or postcode repeated within this batch
      * a name already used by one of this client's active sites
      * rows beyond the client's remaining site allowance
    """
    plan = ImportPlan(preset=preset or client.effective_preset)

    parsed_ok = [row for row in rows if row.ok]
    plan.rejected.extend(row for row in rows if not row.ok)

    coords = geocode_postcodes_bulk(row.postcode for row in parsed_ok)

    existing_names = {
        name.strip().lower()
        for name in Site.objects.filter(
            client=client, is_active=True
        ).values_list("name", flat=True)
    }

    used = Site.objects.filter(client=client, is_active=True).count()
    limit = client.effective_site_limit
    remaining = max(limit - used, 0)

    seen_names = set()
    seen_postcodes = set()

    for row in parsed_ok:
        key = normalise_postcode(row.postcode)
        lat, lon = coords.get(key, (None, None))
        name_key = row.name.strip().lower()

        if lat is None:
            row.error = (
                f"'{row.postcode}' isn't a UK postcode we could find. "
                f"Check it and try again."
            )
        elif name_key in seen_names:
            row.error = f"'{row.name}' appears more than once in this list."
        elif key in seen_postcodes:
            row.error = (
                f"Postcode {row.postcode} appears more than once in this list."
            )
        elif name_key in existing_names:
            row.error = f"You already have a site called '{row.name}'."
        elif len(plan.ready) >= remaining:
            row.error = (
                f"Over your limit of {limit} sites — you have {used} already, "
                f"so there is room for {remaining} more in this import."
            )

        if row.ok:
            seen_names.add(name_key)
            seen_postcodes.add(key)
            plan.ready.append(PlannedSite(row=row, latitude=lat, longitude=lon))
        else:
            plan.rejected.append(row)

    plan.rejected.sort(key=lambda row: row.line_number)
    return plan


# ============================================================
# CREATION
# ============================================================

@transaction.atomic
def create_sites(plan, client, user):
    """
    Create every ready site in `plan`, with its thresholds and audit trail.

    One transaction for the batch, for the same reason site_create uses one
    for a single site: the post_save signal queues each site's first forecast
    via transaction.on_commit, and that run has to find an active
    ThresholdProfile or it is scored against the runner's fallback limits
    instead of this client's own.

    Worth knowing when writing the message that follows this: those queued
    runs are bounded by FORECAST_MAX_CONCURRENT_THREADS (2) per worker in
    sites/signals.py, and anything over that ceiling is deliberately left to
    the scheduled run_forecasts job. A batch of twenty does not produce
    twenty forecasts in the next minute, and the user should not be told it
    will.

    Returns (created sites, skipped rows). The name and cap checks from
    plan_import run again here rather than being trusted from the preview:
    minutes can pass before the user confirms, and in that time a colleague
    may have added a site with the same name or used up the allowance. Under
    unique_together that would be an IntegrityError rolling back the whole
    batch; re-checking turns it into "we created these eighteen, not these
    two, because...".
    """
    values = thresholds_for(plan.preset)
    # Rows that did not name an exposure take the client's typical setting,
    # which is what they were asked for during onboarding. Urban only if
    # they never answered.
    default_exposure = client.effective_exposure
    created = []
    skipped = []

    taken = {
        name.strip().lower()
        for name in Site.objects.filter(
            client=client, is_active=True
        ).values_list("name", flat=True)
    }
    limit = client.effective_site_limit
    remaining = max(limit - len(taken), 0)

    for planned in plan.ready:
        row = planned.row
        name_key = row.name.strip().lower()

        if name_key in taken:
            row.error = f"You already have a site called '{row.name}'."
            skipped.append(row)
            continue
        if len(created) >= remaining:
            row.error = f"Over your limit of {limit} active sites."
            skipped.append(row)
            continue

        taken.add(name_key)

        site = Site.objects.create(
            # Never from posted data — the caller resolves this from the
            # session user.
            client=client,
            name=row.name,
            postcode=normalise_postcode(row.postcode),
            latitude=planned.latitude,
            longitude=planned.longitude,
            elevation=row.elevation,
            exposure=row.exposure or default_exposure,
        )

        ThresholdProfile.objects.create(site=site, created_by=user, **values)

        ChangeLog.objects.create(
            site=site,
            action=ChangeLog.Action.SITE_CREATED,
            user=user,
            details={
                "name": site.name,
                "postcode": site.postcode,
                "source": "bulk_import",
                "preset": plan.preset,
            },
        )
        created.append(site)

    logger.info(
        "Bulk import created %d site(s), skipped %d, for client %s (preset=%s)",
        len(created), len(skipped), client.name, plan.preset,
    )
    return created, skipped
