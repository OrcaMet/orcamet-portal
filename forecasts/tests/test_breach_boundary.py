"""
A value exactly on a cancel limit counts as a breach, everywhere.

The grid counted breaching ensemble members with `>`, while
core.evaluate_thresholds and the map's own marker gate both use `>=`. A gust
landing exactly on the cancel limit therefore stopped work on the pin and on
the site's forecast, but was not counted as a breaching member in the
contour drawn beneath it — the two disagreed at precisely the value a
supervisor sets the limit to.

A limit reads as "this value stops work", not "anything above it does".
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

import numpy as np

from forecasts.engine.core import evaluate_thresholds

RISK_GRID = (
    Path(settings.BASE_DIR)
    / "forecasts" / "management" / "commands" / "risk_grid.py"
)

THRESHOLDS = {
    "wind_mean_caution": 10.0, "wind_mean_cancel": 14.0,
    "gust_caution": 15.0, "gust_cancel": 20.0,
    "precip_caution": 0.7, "precip_cancel": 2.0,
    "temp_min_caution": 1.0, "temp_min_cancel": -2.0,
    "temp_max_caution": 27.0, "temp_max_cancel": 32.0,
}


class CanonicalGateTests(TestCase):
    """What the rest of the portal does, stated so the grid can match it."""

    def test_a_value_exactly_on_the_cancel_limit_cancels(self):
        verdict, limiting = evaluate_thresholds(
            wind=5.0, gust=20.0, precip=0.0, temp=10.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(verdict, "CANCEL")
        self.assertEqual(limiting, "gust")

    def test_a_value_exactly_on_the_caution_limit_cautions(self):
        verdict, _ = evaluate_thresholds(
            wind=5.0, gust=15.0, precip=0.0, temp=10.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(verdict, "CAUTION")

    def test_just_below_is_still_go(self):
        verdict, _ = evaluate_thresholds(
            wind=5.0, gust=14.999, precip=0.0, temp=10.0,
            thresholds=THRESHOLDS,
        )

        self.assertEqual(verdict, "GO")


class GridBreachCountingTests(TestCase):
    """
    The grid's member counting is a vectorised expression inside a long
    command, with no seam to call it through. Asserting on the comparison
    operators it uses is narrow, but it is the thing that was wrong, and it
    fails loudly if someone reverts to `>`.
    """

    def setUp(self):
        self.source = RISK_GRID.read_text(encoding="utf-8")

        block = re.search(
            r"breach = np\.zeros.*?breach_counts\[idx\]",
            self.source, re.S,
        )
        self.assertIsNotNone(block, "member breach block not found")
        self.block = block.group(0)

    def test_wind_gust_and_precip_use_an_inclusive_comparison(self):
        for variable in ("wind_speed_10m", "wind_gusts_10m", "precipitation"):
            with self.subTest(variable=variable):
                self.assertIn(f'stacked["{variable}"] >= ', self.block)

    def test_no_exclusive_comparison_survives(self):
        """`>` alone was the defect; `>=` contains it, so match precisely."""
        self.assertIsNone(
            re.search(r'stacked\["\w+"\] > [^=]', self.block),
            "a strict > comparison is back in the breach count",
        )

    def test_temperature_stays_two_sided_and_inclusive(self):
        self.assertIn('stacked["temperature_2m"] <= ', self.block)
        self.assertIn('stacked["temperature_2m"] >= ', self.block)


class BoundaryAgreementTests(TestCase):
    """
    The two scorers must agree at the boundary, which is the whole point.

    This reproduces the grid's rule directly rather than through the command,
    and checks it against the canonical gate on the exact-limit case.
    """

    def test_the_grid_rule_and_the_gate_agree_on_the_limit(self):
        gust = np.array([20.0])
        cancel = THRESHOLDS["gust_cancel"]

        grid_breaches = bool((gust >= cancel).any())
        verdict, _ = evaluate_thresholds(
            wind=0.0, gust=float(gust[0]), precip=0.0, temp=10.0,
            thresholds=THRESHOLDS,
        )

        self.assertTrue(grid_breaches)
        self.assertEqual(verdict, "CANCEL")
