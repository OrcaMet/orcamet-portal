"""
OrcaMet Portal — Regression tests for the ensemble blend and data-safety fixes.

Each test here pins a specific bug found in the audit:

1. Ensemble weights were normalised across every fetched model, but models
   returning an unusable series were dropped from the sum without having
   their weight removed — biasing blended values low.
2. Missing API readings were substituted with benign defaults (0 m/s wind,
   10 degrees C), so a gap in the feed read as calm, dry and mild.
3. NaN values reached JsonResponse and produced invalid JSON.
4. Longitude 0.0 was treated as "no coordinates".
"""

import json
import math
from unittest import TestCase

import numpy as np

from forecasts.engine.core import _create_weighted_ensemble, _to_float_array


# ============================================================
# Weighted ensemble blending
# ============================================================

class WeightedEnsembleTests(TestCase):

    @staticmethod
    def _model(time, **series):
        data = {"time": time, "wind_speed": [], "wind_gusts": [],
                "precipitation": [], "temperature": []}
        data.update(series)
        return data

    def test_unusable_series_does_not_dilute_the_blend(self):
        """
        A model whose series cannot be used must not contribute weight.

        Previously its weight stayed in the denominator while its values were
        skipped, so the blended value was divided by more weight than was
        applied. With weights 0.6/0.4 and only the 0.6 model usable, the old
        code produced 6.0 for a true value of 10.0 — a 40% under-read.
        """
        time = ["2026-01-01T00:00", "2026-01-01T01:00"]

        ensemble = {
            "good": {
                "weight": 0.6,
                "data": self._model(
                    time,
                    wind_speed=[10.0, 10.0], wind_gusts=[10.0, 10.0],
                    precipitation=[0.0, 0.0], temperature=[5.0, 5.0],
                ),
            },
            "wrong_length": {
                "weight": 0.4,
                # Three values against a two-hour axis — unusable.
                "data": self._model(
                    time,
                    wind_speed=[99.0, 99.0, 99.0], wind_gusts=[99.0, 99.0, 99.0],
                    precipitation=[9.0, 9.0, 9.0], temperature=[9.0, 9.0, 9.0],
                ),
            },
        }

        df = _create_weighted_ensemble(ensemble, ["good", "wrong_length"])

        np.testing.assert_allclose(df["wind_speed"].values, [10.0, 10.0])
        np.testing.assert_allclose(df["temperature"].values, [5.0, 5.0])

    def test_weighted_mean_is_correct_when_all_models_contribute(self):
        time = ["2026-01-01T00:00"]
        ensemble = {
            "a": {"weight": 0.75, "data": self._model(
                time, wind_speed=[10.0], wind_gusts=[10.0],
                precipitation=[0.0], temperature=[0.0])},
            "b": {"weight": 0.25, "data": self._model(
                time, wind_speed=[20.0], wind_gusts=[20.0],
                precipitation=[4.0], temperature=[8.0])},
        }

        df = _create_weighted_ensemble(ensemble, ["a", "b"])

        # 0.75*10 + 0.25*20 = 12.5
        self.assertAlmostEqual(df["wind_speed"].iloc[0], 12.5)
        self.assertAlmostEqual(df["precipitation"].iloc[0], 1.0)
        self.assertAlmostEqual(df["temperature"].iloc[0], 2.0)

    def test_null_hour_reweights_to_the_remaining_models(self):
        """A null for one hour must not drag that hour's mean toward zero."""
        time = ["2026-01-01T00:00", "2026-01-01T01:00"]
        ensemble = {
            "a": {"weight": 0.6, "data": self._model(
                time, wind_speed=[10.0, 10.0], wind_gusts=[10.0, 10.0],
                precipitation=[0.0, 0.0], temperature=[5.0, 5.0])},
            "b": {"weight": 0.4, "data": self._model(
                time, wind_speed=[20.0, None], wind_gusts=[20.0, None],
                precipitation=[0.0, None], temperature=[5.0, None])},
        }

        df = _create_weighted_ensemble(ensemble, ["a", "b"])

        # Hour 0: both models -> 0.6*10 + 0.4*20 = 14.0
        self.assertAlmostEqual(df["wind_speed"].iloc[0], 14.0)
        # Hour 1: only model a -> 6.0/0.6 = 10.0, NOT 6.0
        self.assertAlmostEqual(df["wind_speed"].iloc[1], 10.0)

    def test_hour_with_no_contributing_model_is_nan_not_zero(self):
        """No data must read as unknown, never as calm."""
        time = ["2026-01-01T00:00"]
        ensemble = {
            "a": {"weight": 1.0, "data": self._model(
                time, wind_speed=[None], wind_gusts=[None],
                precipitation=[None], temperature=[None])},
        }

        df = _create_weighted_ensemble(ensemble, ["a"])

        self.assertTrue(math.isnan(df["wind_speed"].iloc[0]))
        self.assertTrue(math.isnan(df["temperature"].iloc[0]))

    def test_spread_is_finite_or_nan_never_raises(self):
        time = ["2026-01-01T00:00", "2026-01-01T01:00"]
        ensemble = {
            "a": {"weight": 0.5, "data": self._model(
                time, wind_speed=[10.0, None], wind_gusts=[10.0, None],
                precipitation=[0.0, None], temperature=[5.0, None])},
            "b": {"weight": 0.5, "data": self._model(
                time, wind_speed=[20.0, None], wind_gusts=[20.0, None],
                precipitation=[0.0, None], temperature=[5.0, None])},
        }

        df = _create_weighted_ensemble(ensemble, ["a", "b"])

        self.assertAlmostEqual(df["wind_speed_spread"].iloc[0], 5.0)
        self.assertTrue(math.isnan(df["wind_speed_spread"].iloc[1]))


class ToFloatArrayTests(TestCase):

    def test_none_becomes_nan(self):
        arr = _to_float_array([1.0, None, 3.0], 3)
        self.assertTrue(math.isnan(arr[1]))
        self.assertEqual(arr[0], 1.0)

    def test_length_mismatch_is_rejected(self):
        self.assertIsNone(_to_float_array([1.0, 2.0], 3))

    def test_missing_series_is_rejected(self):
        self.assertIsNone(_to_float_array(None, 3))


# ============================================================
# Grid: missing readings must not become benign defaults
# ============================================================

class SafeFloatTests(TestCase):

    def test_missing_values_are_nan_not_benign_defaults(self):
        from forecasts.management.commands.risk_grid import _safe_float

        # The old implementation returned 0.0 here (and 10.0 for temperature),
        # which the risk model reads as perfectly safe conditions.
        self.assertTrue(math.isnan(_safe_float(None)))
        self.assertTrue(math.isnan(_safe_float(float("nan"))))
        self.assertTrue(math.isnan(_safe_float(float("inf"))))
        self.assertTrue(math.isnan(_safe_float("not-a-number")))

    def test_real_values_pass_through(self):
        from forecasts.management.commands.risk_grid import _safe_float

        self.assertEqual(_safe_float(0.0), 0.0)
        self.assertEqual(_safe_float(12.5), 12.5)
        self.assertEqual(_safe_float("7"), 7.0)


# ============================================================
# JSON safety
# ============================================================

class JsonSafetyTests(TestCase):

    def test_bare_nan_is_invalid_json(self):
        """Confirms the failure mode the _num guard exists to prevent."""
        payload = json.dumps({"peak_risk": float("nan")})
        self.assertIn("NaN", payload)
        with self.assertRaises(ValueError):
            json.loads(payload, parse_constant=_reject)

    def test_num_replaces_non_finite_with_none(self):
        from dashboard.views import _num

        self.assertIsNone(_num(float("nan")))
        self.assertIsNone(_num(float("inf")))
        self.assertIsNone(_num(float("-inf")))
        self.assertIsNone(_num(None))
        self.assertEqual(_num(0.0), 0.0)
        self.assertEqual(_num(12.5), 12.5)

    def test_sanitised_payload_is_valid_json(self):
        from dashboard.views import _num

        payload = json.dumps({"peak_risk": _num(float("nan"))})
        self.assertEqual(json.loads(payload), {"peak_risk": None})


def _reject(value):
    raise ValueError(f"invalid JSON constant: {value}")


# ============================================================
# Greenwich meridian coordinates
# ============================================================

class SiteCoordsTests(TestCase):

    def test_zero_longitude_is_a_valid_coordinate(self):
        """
        Longitude 0.0 runs through London, Cambridge and East Anglia. The
        old truthiness check treated it as missing, hiding those sites from
        the map and skipping their forecasts entirely.
        """
        from sites.models import Site

        site = Site(name="Greenwich", postcode="SE10 9NF",
                    latitude=51.4779, longitude=0.0)
        self.assertEqual(site.coords, (51.4779, 0.0))

    def test_zero_latitude_is_also_valid(self):
        from sites.models import Site

        site = Site(name="Null Island", postcode="X", latitude=0.0, longitude=0.0)
        self.assertEqual(site.coords, (0.0, 0.0))

    def test_missing_coordinates_still_return_none(self):
        from sites.models import Site

        self.assertIsNone(Site(name="A", postcode="X").coords)
        self.assertIsNone(Site(name="B", postcode="X", latitude=51.5).coords)
        self.assertIsNone(Site(name="C", postcode="X", longitude=-1.0).coords)
