"""
Tests for the admin-editable UK map risk thresholds.

Two things matter: that the singleton stays a singleton, and that an
ordering the risk engine cannot interpret is refused at the form rather
than silently producing a flat ramp.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from forecasts.models import MapThresholds


class MapThresholdsModelTests(TestCase):
    def test_load_creates_the_row_with_engine_defaults(self):
        obj = MapThresholds.load()

        self.assertEqual(obj.pk, 1)
        self.assertEqual(obj.as_dict(), {
            "wind_mean_caution": 10.0, "wind_mean_cancel": 14.0,
            "gust_caution": 15.0, "gust_cancel": 20.0,
            "precip_caution": 0.7, "precip_cancel": 2.0,
            "temp_min_caution": 1.0, "temp_min_cancel": -2.0,
        })

    def test_load_is_idempotent(self):
        first = MapThresholds.load()
        first.gust_cancel = 25.0
        first.save()

        second = MapThresholds.load()

        self.assertEqual(MapThresholds.objects.count(), 1)
        self.assertEqual(second.gust_cancel, 25.0)

    def test_saving_a_new_instance_overwrites_the_singleton(self):
        MapThresholds.load()
        MapThresholds(gust_cancel=30.0).save()

        self.assertEqual(MapThresholds.objects.count(), 1)
        self.assertEqual(MapThresholds.load().gust_cancel, 30.0)

    def test_deleting_is_refused(self):
        obj = MapThresholds.load()
        with self.assertRaises(ValidationError):
            obj.delete()
        self.assertEqual(MapThresholds.objects.count(), 1)

    def test_as_dict_matches_what_the_risk_engine_expects(self):
        """A renamed key would make calculate_hourly_risk raise KeyError."""
        from forecasts.engine.core import calculate_hourly_risk

        risk = calculate_hourly_risk(12.0, 18.0, 1.0, 5.0, MapThresholds.load().as_dict())
        self.assertTrue(0.0 <= risk <= 100.0)


class MapThresholdsValidationTests(TestCase):
    def _obj(self, **overrides):
        obj = MapThresholds.load()
        for k, v in overrides.items():
            setattr(obj, k, v)
        return obj

    def test_defaults_are_valid(self):
        self._obj().full_clean()

    def test_cancel_below_caution_is_refused_for_high_bad_variables(self):
        for field, caution, bad_cancel in (
            ("wind_mean", 10.0, 8.0),
            ("gust", 15.0, 12.0),
            ("precip", 0.7, 0.5),
        ):
            with self.subTest(field=field):
                obj = self._obj(**{
                    f"{field}_caution": caution, f"{field}_cancel": bad_cancel,
                })
                with self.assertRaises(ValidationError) as ctx:
                    obj.full_clean()
                self.assertIn(f"{field}_cancel", ctx.exception.error_dict)

    def test_equal_caution_and_cancel_is_refused(self):
        """A zero-width ramp is a step function, almost certainly a mistake."""
        obj = self._obj(gust_caution=15.0, gust_cancel=15.0)
        with self.assertRaises(ValidationError):
            obj.full_clean()

    def test_temperature_ordering_is_inverted(self):
        """Colder is worse, so cancel must sit below caution."""
        obj = self._obj(temp_min_caution=1.0, temp_min_cancel=3.0)
        with self.assertRaises(ValidationError) as ctx:
            obj.full_clean()
        self.assertIn("temp_min_cancel", ctx.exception.error_dict)

    def test_valid_tightening_is_accepted(self):
        obj = self._obj(
            wind_mean_caution=8.0, wind_mean_cancel=11.0,
            gust_caution=12.0, gust_cancel=16.0,
            temp_min_caution=3.0, temp_min_cancel=0.0,
        )
        obj.full_clean()


class MapThresholdsAdminTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(
            username="steve", email="steve@example.com", password="test-password",
        )
        self.client.force_login(self.staff)

    def test_changelist_redirects_to_the_single_settings_form(self):
        response = self.client.get(reverse("admin:forecasts_mapthresholds_changelist"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("admin:forecasts_mapthresholds_change", args=[1]),
        )

    def test_settings_form_renders(self):
        MapThresholds.load()
        response = self.client.get(
            reverse("admin:forecasts_mapthresholds_change", args=[1])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "gust_cancel")

    def test_adding_a_second_row_is_not_offered(self):
        response = self.client.get(reverse("admin:forecasts_mapthresholds_add"))
        self.assertEqual(response.status_code, 403)

    def test_saving_records_the_editor_and_warns_about_the_rebuild(self):
        MapThresholds.load()
        response = self.client.post(
            reverse("admin:forecasts_mapthresholds_change", args=[1]),
            {
                "wind_mean_caution": 8.0, "wind_mean_cancel": 11.0,
                "gust_caution": 12.0, "gust_cancel": 16.0,
                "precip_caution": 0.5, "precip_cancel": 1.5,
                "temp_min_caution": 3.0, "temp_min_cancel": 0.0,
            },
            follow=True,
        )

        obj = MapThresholds.load()
        self.assertEqual(obj.gust_cancel, 16.0)
        self.assertEqual(obj.updated_by, self.staff)
        self.assertContains(response, "until the grid is rebuilt")

    def test_invalid_ordering_is_rejected_by_the_form(self):
        MapThresholds.load()
        response = self.client.post(
            reverse("admin:forecasts_mapthresholds_change", args=[1]),
            {
                "wind_mean_caution": 10.0, "wind_mean_cancel": 14.0,
                "gust_caution": 15.0, "gust_cancel": 12.0,
                "precip_caution": 0.7, "precip_cancel": 2.0,
                "temp_min_caution": 1.0, "temp_min_cancel": -2.0,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cancel must be higher than caution")
        self.assertEqual(MapThresholds.load().gust_cancel, 20.0)
