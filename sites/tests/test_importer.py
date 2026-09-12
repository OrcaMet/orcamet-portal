"""
Tests for the bulk site importer.

Parsing is tested without touching the network or the database — that is the
point of keeping sites/importer.py free of request handling. The planning and
creation tests stub the bulk geocoder, because what is being tested is what
we do with the answer, not postcodes.io.
"""

from io import BytesIO
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase

from accounts.models import User
from sites.importer import (
    MAX_ROWS, create_sites, parse_rows, parse_upload, plan_import,
)
from sites.models import ChangeLog, Client, Site, ThresholdProfile
from sites.presets import thresholds_for
from sites.signals import trigger_forecast_on_site_save


# Coordinates are irrelevant to every assertion here; they only have to be
# present and distinguishable.
COORDS = {
    "EH11YZ": (55.9510, -3.1890),
    "AB115DQ": (57.1430, -2.0810),
    "LL554TY": (53.0680, -4.0760),
    "G11AB": (55.8610, -4.2500),
}


def fake_bulk(postcodes):
    """Stand-in for geocode_postcodes_bulk with a fixed known universe."""
    from sites.models import normalise_postcode

    out = {}
    for raw in postcodes:
        key = normalise_postcode(raw)
        if key:
            out[key] = COORDS.get(key, (None, None))
    return out


class ParseRowsTests(TestCase):
    """Parsing is forgiving about format and specific about failure."""

    def test_name_and_postcode(self):
        rows = parse_rows("Tower Block A, EH1 1YZ")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].ok)
        self.assertEqual(rows[0].name, "Tower Block A")
        self.assertEqual(rows[0].postcode, "EH1 1YZ")

    def test_tab_separated_excel_paste(self):
        rows = parse_rows("Tower Block A\tEH1 1YZ\nHarbour Crane\tAB11 5DQ")
        self.assertEqual([row.name for row in rows],
                         ["Tower Block A", "Harbour Crane"])
        self.assertTrue(all(row.ok for row in rows))

    def test_comma_inside_a_quoted_name_survives(self):
        rows = parse_rows('"Tower Block A, rear elevation", EH1 1YZ')
        self.assertEqual(rows[0].name, "Tower Block A, rear elevation")
        self.assertEqual(rows[0].postcode, "EH1 1YZ")

    def test_bare_postcode_names_itself(self):
        rows = parse_rows("EH1 1YZ")
        self.assertTrue(rows[0].ok)
        self.assertEqual(rows[0].name, "EH11YZ")
        self.assertEqual(rows[0].postcode, "EH1 1YZ")

    def test_reversed_columns_are_detected(self):
        """A spreadsheet with postcode first is common enough to handle."""
        rows = parse_rows("EH1 1YZ, Tower Block A")
        self.assertTrue(rows[0].ok)
        self.assertEqual(rows[0].name, "Tower Block A")
        self.assertEqual(rows[0].postcode, "EH1 1YZ")

    def test_exposure_and_elevation(self):
        rows = parse_rows("Ridge Mast, LL55 4TY, Highland, 340")
        self.assertTrue(rows[0].ok, rows[0].error)
        self.assertEqual(rows[0].exposure, "highland")
        self.assertEqual(rows[0].elevation, 340)

    def test_elevation_with_unit_suffix(self):
        rows = parse_rows("Ridge Mast, LL55 4TY, Highland, 340m")
        self.assertEqual(rows[0].elevation, 340)

    def test_unknown_exposure_is_rejected_by_name(self):
        rows = parse_rows("Ridge Mast, LL55 4TY, Alpine")
        self.assertFalse(rows[0].ok)
        self.assertIn("Alpine", rows[0].error)

    def test_non_numeric_elevation_is_rejected(self):
        rows = parse_rows("Ridge Mast, LL55 4TY, Highland, high up")
        self.assertFalse(rows[0].ok)
        self.assertIn("high up", rows[0].error)

    def test_blank_lines_are_skipped_silently(self):
        rows = parse_rows("Tower A, EH1 1YZ\n\n   \n\nTower B, AB11 5DQ")
        self.assertEqual(len(rows), 2)

    def test_header_row_is_skipped(self):
        rows = parse_rows("Name,Postcode\nTower A,EH1 1YZ")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Tower A")

    def test_line_with_no_postcode_is_reported_not_dropped(self):
        rows = parse_rows("Tower A, somewhere in Fife")
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].ok)
        self.assertIn("postcode", rows[0].error.lower())

    def test_line_numbers_point_at_the_original_text(self):
        """A reported error is only useful if it names the right line."""
        rows = parse_rows("Tower A, EH1 1YZ\n\nrubbish\nTower B, AB11 5DQ")
        bad = [row for row in rows if not row.ok]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].line_number, 3)

    def test_rows_beyond_the_cap_are_reported_not_silently_dropped(self):
        text = "\n".join(f"Site {n}, EH1 1YZ" for n in range(MAX_ROWS + 5))
        rows = parse_rows(text)
        over = [row for row in rows if not row.ok]
        self.assertTrue(over)
        self.assertIn(str(MAX_ROWS), over[0].error)

    def test_empty_input(self):
        self.assertEqual(parse_rows(""), [])
        self.assertEqual(parse_rows(None), [])


class ParseUploadTests(TestCase):
    def test_csv_with_bom(self):
        """Excel writes a BOM; it must not end up in the first site's name."""
        data = "﻿Name,Postcode\nTower A,EH1 1YZ\n".encode("utf-8")
        rows = parse_upload(BytesIO(data))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Tower A")

    def test_cp1252_fallback(self):
        data = "Café Tower, EH1 1YZ".encode("cp1252")
        rows = parse_upload(BytesIO(data))
        self.assertTrue(rows[0].ok)
        self.assertIn("Tower", rows[0].name)


@patch("sites.importer.geocode_postcodes_bulk", side_effect=fake_bulk)
class PlanImportTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(
            name="Summit Rope Access", self_service_sites=True, site_limit=10,
        )

    def test_good_rows_are_ready_with_coordinates(self, _mock):
        plan = plan_import(parse_rows("Tower A, EH1 1YZ"), self.client_obj)
        self.assertEqual(plan.ready_count, 1)
        self.assertAlmostEqual(plan.ready[0].latitude, 55.9510)

    def test_unknown_postcode_is_rejected(self, _mock):
        plan = plan_import(parse_rows("Tower A, XX99 9ZZ"), self.client_obj)
        self.assertEqual(plan.ready_count, 0)
        self.assertIn("XX99 9ZZ", plan.rejected[0].error)

    def test_duplicate_name_within_the_batch(self, _mock):
        plan = plan_import(
            parse_rows("Tower A, EH1 1YZ\nTower A, AB11 5DQ"), self.client_obj
        )
        self.assertEqual(plan.ready_count, 1)
        self.assertIn("more than once", plan.rejected[0].error)

    def test_duplicate_postcode_within_the_batch(self, _mock):
        plan = plan_import(
            parse_rows("Tower A, EH1 1YZ\nTower B, EH1 1YZ"), self.client_obj
        )
        self.assertEqual(plan.ready_count, 1)
        self.assertIn("more than once", plan.rejected[0].error)

    def test_name_already_used_by_this_client(self, _mock):
        Site.objects.create(
            client=self.client_obj, name="Tower A", postcode="G1 1AB",
            latitude=55.86, longitude=-4.25,
        )
        plan = plan_import(parse_rows("Tower A, EH1 1YZ"), self.client_obj)
        self.assertEqual(plan.ready_count, 0)
        self.assertIn("already have a site", plan.rejected[0].error)

    def test_another_clients_name_does_not_clash(self, _mock):
        other = Client.objects.create(name="Someone Else")
        Site.objects.create(
            client=other, name="Tower A", postcode="G1 1AB",
            latitude=55.86, longitude=-4.25,
        )
        plan = plan_import(parse_rows("Tower A, EH1 1YZ"), self.client_obj)
        self.assertEqual(plan.ready_count, 1)

    def test_batch_that_exactly_fills_the_cap(self, _mock):
        self.client_obj.site_limit = 2
        self.client_obj.save()
        plan = plan_import(
            parse_rows("Tower A, EH1 1YZ\nTower B, AB11 5DQ"), self.client_obj
        )
        self.assertEqual(plan.ready_count, 2)
        self.assertEqual(plan.rejected_count, 0)

    def test_batch_that_overruns_the_cap_reports_the_overflow(self, _mock):
        self.client_obj.site_limit = 2
        self.client_obj.save()
        plan = plan_import(
            parse_rows(
                "Tower A, EH1 1YZ\nTower B, AB11 5DQ\nTower C, LL55 4TY"
            ),
            self.client_obj,
        )
        self.assertEqual(plan.ready_count, 2)
        self.assertEqual(plan.rejected_count, 1)
        self.assertIn("limit of 2", plan.rejected[0].error)

    def test_cap_counts_sites_the_client_already_has(self, _mock):
        self.client_obj.site_limit = 2
        self.client_obj.save()
        Site.objects.create(
            client=self.client_obj, name="Existing", postcode="G1 1AB",
            latitude=55.86, longitude=-4.25,
        )
        plan = plan_import(
            parse_rows("Tower A, EH1 1YZ\nTower B, AB11 5DQ"), self.client_obj
        )
        self.assertEqual(plan.ready_count, 1)

    def test_rejected_rows_are_ordered_by_line_number(self, _mock):
        plan = plan_import(
            parse_rows("rubbish\nTower A, XX99 9ZZ\nalso rubbish"),
            self.client_obj,
        )
        numbers = [row.line_number for row in plan.rejected]
        self.assertEqual(numbers, sorted(numbers))

    def test_session_round_trip_preserves_the_plan(self, _mock):
        from sites.importer import ImportPlan

        plan = plan_import(
            parse_rows("Ridge Mast, LL55 4TY, Highland, 340"),
            self.client_obj, preset="crane_lifting",
        )
        restored = ImportPlan.from_session(plan.to_session())

        self.assertEqual(restored.preset, "crane_lifting")
        self.assertEqual(restored.ready_count, 1)
        self.assertEqual(restored.ready[0].row.name, "Ridge Mast")
        self.assertEqual(restored.ready[0].row.elevation, 340)
        self.assertEqual(restored.ready[0].row.exposure, "highland")
        self.assertAlmostEqual(restored.ready[0].latitude, 53.0680)

    def test_a_corrupt_session_plan_is_refused_rather_than_half_created(self, _mock):
        from sites.importer import ImportPlan

        self.assertIsNone(ImportPlan.from_session(None))
        self.assertIsNone(ImportPlan.from_session("nonsense"))
        self.assertIsNone(ImportPlan.from_session(
            {"ready": [{"name": "Tower A"}]}  # no postcode or coordinates
        ))


@patch("sites.importer.geocode_postcodes_bulk", side_effect=fake_bulk)
class CreateSitesTests(TestCase):
    """
    Creation, with the forecast signal disconnected.

    Left connected it would start real background threads against
    Open-Meteo — see the same pattern in dashboard/tests.
    """

    def setUp(self):
        post_save.disconnect(trigger_forecast_on_site_save, sender=Site)
        self.addCleanup(
            post_save.connect, trigger_forecast_on_site_save, sender=Site
        )
        self.client_obj = Client.objects.create(
            name="Summit Rope Access", self_service_sites=True, site_limit=10,
        )
        self.user = User.objects.create_user(
            username="dave", email="dave@example.com",
            role=User.Role.CLIENT_ADMIN, client=self.client_obj,
        )

    def _create(self, text, preset="rope_access"):
        plan = plan_import(parse_rows(text), self.client_obj, preset=preset)
        return create_sites(plan, self.client_obj, self.user)

    def test_creates_sites_with_coordinates_and_attributes(self, _mock):
        created, skipped = self._create("Ridge Mast, LL55 4TY, Highland, 340")

        self.assertEqual(len(created), 1)
        self.assertEqual(skipped, [])
        site = created[0]
        self.assertEqual(site.client, self.client_obj)
        self.assertEqual(site.postcode, "LL554TY")
        self.assertEqual(site.exposure, "highland")
        self.assertEqual(site.elevation, 340)
        self.assertAlmostEqual(site.latitude, 53.0680)

    def test_every_site_gets_one_active_profile_from_the_preset(self, _mock):
        """
        The invariant the forecast engine depends on. Without an active
        profile a run is scored against the runner's fallback limits, which
        is wrong silently.
        """
        created, _ = self._create(
            "Tower A, EH1 1YZ\nTower B, AB11 5DQ", preset="crane_lifting"
        )
        expected = thresholds_for("crane_lifting")

        for site in created:
            profiles = ThresholdProfile.objects.filter(site=site, is_active=True)
            self.assertEqual(profiles.count(), 1)
            self.assertEqual(profiles.first().as_dict(), expected)

    def test_an_unknown_preset_falls_back_rather_than_crashing(self, _mock):
        created, _ = self._create("Tower A, EH1 1YZ", preset="not-a-preset")
        profile = ThresholdProfile.objects.get(site=created[0])
        self.assertEqual(profile.as_dict(), thresholds_for("rope_access"))

    def test_writes_a_changelog_entry_per_site(self, _mock):
        created, _ = self._create("Tower A, EH1 1YZ\nTower B, AB11 5DQ")
        for site in created:
            entry = ChangeLog.objects.get(site=site)
            self.assertEqual(entry.action, ChangeLog.Action.SITE_CREATED)
            self.assertEqual(entry.details["source"], "bulk_import")
            self.assertEqual(entry.user, self.user)

    def test_a_name_taken_since_the_preview_is_skipped_not_fatal(self, _mock):
        """
        Minutes can pass between preview and confirm. Under unique_together a
        clash would be an IntegrityError taking the whole batch down; it
        should cost one row instead.
        """
        plan = plan_import(
            parse_rows("Tower A, EH1 1YZ\nTower B, AB11 5DQ"), self.client_obj
        )
        # A colleague gets there first.
        Site.objects.create(
            client=self.client_obj, name="Tower A", postcode="G1 1AB",
            latitude=55.86, longitude=-4.25,
        )

        created, skipped = create_sites(plan, self.client_obj, self.user)

        self.assertEqual([site.name for site in created], ["Tower B"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("already have a site", skipped[0].error)

    def test_the_cap_is_rechecked_at_creation(self, _mock):
        plan = plan_import(
            parse_rows("Tower A, EH1 1YZ\nTower B, AB11 5DQ"), self.client_obj
        )
        self.client_obj.site_limit = 1
        self.client_obj.save()

        created, skipped = create_sites(plan, self.client_obj, self.user)

        self.assertEqual(len(created), 1)
        self.assertEqual(len(skipped), 1)
