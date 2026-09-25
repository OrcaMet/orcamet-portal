"""
Every script and stylesheet loaded from another origin carries an integrity
hash, so a compromised CDN cannot run code inside a logged-in session.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

TEMPLATE_DIRS = [
    Path(settings.BASE_DIR) / app / "templates"
    for app in ("orcamet_portal", "accounts", "sites", "dashboard", "forecasts")
]

EXTERNAL_TAG = re.compile(
    r"<(script|link)\b[^>]*\b(?:src|href)=\"https?://[^\"]+\"[^>]*>",
    re.IGNORECASE,
)


def _external_tags():
    for directory in TEMPLATE_DIRS:
        for path in directory.rglob("*.html"):
            for match in EXTERNAL_TAG.finditer(path.read_text(encoding="utf-8")):
                tag = match.group(0)
                # Only executable or style resources; a plain hyperlink or a
                # preconnect hint loads nothing that runs.
                if match.group(1).lower() == "link" and 'rel="stylesheet"' not in tag:
                    continue
                yield path.name, tag


class SubresourceIntegrityTests(SimpleTestCase):
    def test_there_are_external_resources_to_check(self):
        """Guard against the regex silently matching nothing."""
        self.assertGreaterEqual(len(list(_external_tags())), 7)

    def test_every_external_resource_has_an_integrity_hash(self):
        missing = [
            f"{name}: {tag}" for name, tag in _external_tags()
            if 'integrity="sha384-' not in tag
        ]
        self.assertEqual(missing, [])

    def test_every_external_resource_is_fetched_anonymously(self):
        """Integrity checks on cross-origin files need a CORS request."""
        missing = [
            f"{name}: {tag}" for name, tag in _external_tags()
            if 'crossorigin="anonymous"' not in tag
        ]
        self.assertEqual(missing, [])
