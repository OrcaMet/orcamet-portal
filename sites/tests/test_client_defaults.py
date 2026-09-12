"""
Tests for the per-client defaults applied to new sites.

These exist because both defaults used to live in the onboarding session,
where nothing outside the wizard could see them. The consequence was that a
site added singly — the day after onboarding, or six months later — was
created with the model's field defaults rather than the limits the client
chose, and an imported row without an exposure was always Urban no matter
what they had answered.

The property under test in most of these is "a site created by any path gets
the same defaults".
"""

from unittest.mock import patch

from django.test import TestCase, override_settings

from accounts.models import User
from sites.importer import create_sites, parse_rows, plan_import
from sites.models import Client, Site, ThresholdProfile
from sites.presets import DEFAULT_PRESET, thresholds_for

COORDS = {"EH11YZ": (55.951, -3.189), "AB115DQ": (57.143, -2.081)}


def fake_bulk(postcodes):
    from sites.models import normalise_postcode

    return {
        normalise_postcode(p): COORDS.get(normalise_postcode(p), (None, None))
        for p in postcodes if normalise_postcode(p)
    }


def make_user(**client_kwargs):
    client_kwargs.setdefault("name", "Summit Rope Access")
    client_kwargs.setdefault("self_service_sites", True)
    client_kwargs.setdefault("site_limit", 10)
    client = Client.objects.create(**client_kwargs)
    return User.objects.create_user(
        username="jo", email="jo@example.com",
        role=User.Role.CLIENT_ADMIN, client=client,
    )


class EffectiveDefaultsTests(TestCase):
    """
    Both fields are plain CharFields with no choices — Client is declared
    before Site — so the accessors have to cope with blank and with rubbish.
    """

    def test_blank_falls_back(self):
        client = Client.objects.create(name="Acme")
        self.assertEqual(client.effective_preset, DEFAULT_PRESET)
        self.assertEqual(client.effective_exposure, Site.Exposure.URBAN)

    def test_a_set_value_is_used(self):
        client = Client.objects.create(
            name="Acme", threshold_preset="crane_lifting",
            default_exposure="coastal",
        )
        self.assertEqual(client.effective_preset, "crane_lifting")
        self.assertEqual(client.effective_exposure, "coastal")

    def test_an_unknown_value_falls_back_rather_than_raising(self):
        """A preset renamed in code must not break every client that had it."""
        client = Client.objects.create(
            name="Acme", threshold_preset="preset-that-was-deleted",
            default_exposure="lunar",
        )
        self.assertEqual(client.effective_preset, DEFAULT_PRESET)
        self.assertEqual(client.effective_exposure, Site.Exposure.URBAN)

    def test_exposure_label(self):
        client = Client.objects.create(name="Acme", default_exposure="highland")
        self.assertEqual(client.effective_exposure_label, "Highland")


@patch("sites.signals.queue_forecast_generation", return_value=True)
@patch("sites.importer.geocode_postcodes_bulk", side_effect=fake_bulk)
class ImportUsesClientDefaultsTests(TestCase):
    def test_rows_without_an_exposure_take_the_clients_setting(self, _geo, _queue):
        user = make_user(default_exposure="coastal")
        plan = plan_import(parse_rows("Tower A, EH1 1YZ"), user.client)

        created, _ = create_sites(plan, user.client, user)

        self.assertEqual(created[0].exposure, "coastal")

    def test_a_row_that_names_its_exposure_still_wins(self, _geo, _queue):
        user = make_user(default_exposure="coastal")
        plan = plan_import(
            parse_rows("Tower A, EH1 1YZ, Highland"), user.client
        )

        created, _ = create_sites(plan, user.client, user)

        self.assertEqual(created[0].exposure, "highland")

    def test_an_import_with_no_preset_uses_the_clients(self, _geo, _queue):
        user = make_user(threshold_preset="conservative")

        plan = plan_import(parse_rows("Tower A, EH1 1YZ"), user.client)

        self.assertEqual(plan.preset, "conservative")
        created, _ = create_sites(plan, user.client, user)
        profile = ThresholdProfile.objects.get(site=created[0], is_active=True)
        self.assertEqual(profile.as_dict(), thresholds_for("conservative"))


@override_settings(SANDBOX_MAX_SITES=10)
@patch("sites.signals.queue_forecast_generation", return_value=True)
@patch("sites.models.geocode_postcode", return_value=(55.95, -3.19))
class SingleSiteAddUsesClientDefaultsTests(TestCase):
    """
    The bug this fixes: adding one site by hand ignored the client's preset
    and silently used ThresholdProfile's field defaults.
    """

    def _add(self, user, **overrides):
        self.client.force_login(user)
        data = {
            "name": "Tower Block A", "postcode": "EH1 1YZ",
            "exposure": "urban", "elevation": 0, "notes": "",
        }
        data.update(overrides)
        return self.client.post("/sites/add/", data)

    def test_the_new_sites_thresholds_come_from_the_clients_preset(self, _geo, _queue):
        user = make_user(threshold_preset="crane_lifting")

        self._add(user)

        site = Site.objects.get()
        profile = ThresholdProfile.objects.get(site=site, is_active=True)
        self.assertEqual(profile.as_dict(), thresholds_for("crane_lifting"))

    def test_it_matches_what_an_import_would_have_produced(self, _geo, _queue):
        """
        The property that actually matters to a client: two sites added two
        different ways are scored against the same limits.
        """
        user = make_user(threshold_preset="conservative")

        self._add(user, name="Added by hand")
        with patch("sites.importer.geocode_postcodes_bulk", side_effect=fake_bulk):
            plan = plan_import(parse_rows("Imported, AB11 5DQ"), user.client)
            create_sites(plan, user.client, user)

        limits = {
            site.name: ThresholdProfile.objects.get(
                site=site, is_active=True
            ).as_dict()
            for site in Site.objects.all()
        }
        self.assertEqual(limits["Added by hand"], limits["Imported"])

    def test_a_client_with_no_preset_still_gets_the_default(self, _geo, _queue):
        user = make_user()

        self._add(user)

        profile = ThresholdProfile.objects.get(is_active=True)
        self.assertEqual(profile.as_dict(), thresholds_for(DEFAULT_PRESET))

    def test_the_form_opens_on_the_clients_typical_exposure(self, _geo, _queue):
        user = make_user(default_exposure="highland")
        self.client.force_login(user)

        response = self.client.get("/sites/add/")

        self.assertEqual(
            response.context["form"].initial["exposure"], "highland"
        )
