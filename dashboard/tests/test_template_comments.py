"""
Django's {# ... #} comment is single-line only. Spread over several lines it
is not a comment at all: it renders as text at the top of the page. A
multi-line note has to be {% comment %} ... {% endcomment %}.
"""

from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from accounts.models import User
from forecasts.models import ForecastRun
from sites.models import Client, Site

TEMPLATE_DIRS = [
    Path(settings.BASE_DIR) / app / "templates"
    for app in ("orcamet_portal", "accounts", "sites", "dashboard", "forecasts")
]


class SingleLineCommentTests(SimpleTestCase):
    def test_no_template_opens_a_comment_it_does_not_close_on_the_same_line(self):
        offenders = []
        for directory in TEMPLATE_DIRS:
            for path in directory.rglob("*.html"):
                for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if "{#" in line and "#}" not in line[line.index("{#"):]:
                        offenders.append(f"{path.name}:{n}")
        self.assertEqual(offenders, [])


@patch("sites.signals.queue_forecast_generation", return_value=True)
class RenderedPagesTests(TestCase):
    def test_no_comment_markers_leak_into_pages(self, _q):
        client = Client.objects.create(name="Acme Rope")
        site = Site.objects.create(
            client=client, name="Tower", postcode="EH1 1YZ",
            latitude=55.95, longitude=-3.19,
        )
        ForecastRun.objects.create(
            site=site, forecast_date=timezone.localdate(), status="success",
            recommendation="GO", peak_risk=5.0, models_used=[],
        )
        user = User.objects.create_user(
            username="dana", role=User.Role.CLIENT_ADMIN, client=client,
        )
        self.client.force_login(user)

        for url in ("/dashboard/", f"/dashboard/site/{site.pk}/", "/dashboard/map/"):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertNotIn("{#", html)
                self.assertNotIn("#}", html)
