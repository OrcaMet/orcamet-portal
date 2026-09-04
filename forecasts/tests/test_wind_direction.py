"""
Wind direction is averaged as a vector, never as an angle.

The arithmetic mean of 350 and 10 degrees is 180 — a southerly, the exact
opposite of the northerly both members forecast. Any code that averages
compass bearings as numbers produces its worst errors precisely where the
members agree most strongly, which is the last place anyone would look.

Direction is stored the way meteorology states it: the direction the wind
comes FROM. That convention has to survive intact from the API through the
database to the arrow on screen, because an arrow drawn backwards would send
a supervisor to the windward face of a building believing it was the lee.
"""

import re
from pathlib import Path

import numpy as np
from django.conf import settings
from django.test import TestCase

from forecasts.engine.ensemble import (
    MEMBER_VARIABLES,
    UNGATED_VARIABLES,
    VARIABLES,
    _attach_ungated,
)

TEMPLATE = (
    Path(settings.BASE_DIR)
    / "dashboard" / "templates" / "dashboard" / "weather_map.html"
)
RISK_GRID = (
    Path(settings.BASE_DIR)
    / "forecasts" / "management" / "commands" / "risk_grid.py"
)


def _vector_mean(directions, speeds):
    """The averaging rule risk_grid uses, isolated for testing."""
    radians = np.radians(np.asarray(directions, dtype=float))
    weights = np.asarray(speeds, dtype=float)

    u = (weights * np.sin(radians)).sum()
    v = (weights * np.cos(radians)).sum()

    direction = np.degrees(np.arctan2(u, v)) % 360.0
    # Matches how the command stores it. Due north returns from arctan2 as a
    # hair below zero, which the modulo lifts to 359.99... and rounding then
    # turns into 360.0 — outside the range the field documents.
    direction = round(float(direction), 1) % 360
    agreement = np.hypot(u, v) / weights.sum()
    return direction, agreement


class VectorAveragingTests(TestCase):

    def test_the_wrap_around_case(self):
        """350 and 10 average to north, not south. The whole point."""
        direction, _ = _vector_mean([350.0, 10.0], [10.0, 10.0])

        self.assertAlmostEqual(direction, 0.0, places=6)

    def test_due_north_is_stored_as_zero_not_360(self):
        """Both mean north, but the field documents 0-359.9."""
        for directions in ([0.0, 0.0], [350.0, 10.0], [359.0, 1.0]):
            with self.subTest(directions=directions):
                direction, _ = _vector_mean(directions, [10.0, 10.0])
                self.assertGreaterEqual(direction, 0.0)
                self.assertLess(direction, 360.0)

    def test_a_naive_mean_would_have_failed_that(self):
        """Stated so the test above cannot be satisfied by accident."""
        self.assertAlmostEqual(np.mean([350.0, 10.0]), 180.0)

    def test_unanimous_members_keep_their_direction(self):
        direction, agreement = _vector_mean([270.0] * 51, [12.0] * 51)

        self.assertAlmostEqual(direction, 270.0, places=6)
        self.assertAlmostEqual(agreement, 1.0, places=6)

    def test_opposed_members_have_no_agreement(self):
        """Two members exactly opposed cancel: the mean means nothing."""
        _, agreement = _vector_mean([0.0, 180.0], [10.0, 10.0])

        self.assertAlmostEqual(agreement, 0.0, places=6)

    def test_scattered_members_score_low(self):
        _, agreement = _vector_mean(
            [0.0, 90.0, 180.0, 270.0], [10.0, 10.0, 10.0, 10.0]
        )

        self.assertLess(agreement, 0.1)

    def test_the_strong_members_set_the_direction(self):
        """
        Speed-weighted: a 2 m/s easterly breeze must not pull the arrow away
        from thirty members forecasting a 20 m/s westerly.
        """
        directions = [270.0] * 30 + [90.0]
        speeds = [20.0] * 30 + [2.0]

        direction, _ = _vector_mean(directions, speeds)

        self.assertAlmostEqual(direction, 270.0, places=4)

    def test_the_compass_convention_is_preserved(self):
        """270 degrees is a westerly — wind FROM the west."""
        direction, _ = _vector_mean([270.0], [10.0])
        self.assertAlmostEqual(direction, 270.0, places=6)

        # atan2(u, v) with u east and v north must give a bearing, not the
        # mathematical angle: due east is 90, not 0.
        direction, _ = _vector_mean([90.0], [10.0])
        self.assertAlmostEqual(direction, 90.0, places=6)


class GridImplementationTests(TestCase):
    """
    The command accumulates into numpy arrays inside a long loop with no
    seam to call. Assert on the expressions it uses, so a revert to angle
    averaging fails loudly.
    """

    def setUp(self):
        self.source = RISK_GRID.read_text(encoding="utf-8")

    def test_it_accumulates_components_not_degrees(self):
        self.assertIn("acc_dir_u", self.source)
        self.assertIn("acc_dir_v", self.source)
        self.assertIn("np.sin(radians)", self.source)
        self.assertIn("np.cos(radians)", self.source)

    def test_it_resolves_with_arctan2(self):
        self.assertIn("np.arctan2(acc_dir_u, acc_dir_v)", self.source)

    def test_the_bearing_is_wrapped_into_a_full_circle(self):
        self.assertIn("% 360.0", self.source)

    def test_direction_is_weighted_by_speed(self):
        block = re.search(
            r"usable = np\.isfinite\(dirs\).*?wt_dir\[idx\] \+=",
            self.source, re.S,
        )
        self.assertIsNotNone(block, "direction accumulation block not found")
        self.assertIn("weight * np.sin", block.group(0))


class UngatedVariableTests(TestCase):
    """
    Direction is fetched but never gated: no direction is unsafe by itself,
    and a model that declines to report it must still give a usable forecast.
    """

    def test_direction_is_requested(self):
        self.assertIn("wind_direction_10m", MEMBER_VARIABLES)

    def test_it_is_not_a_gate_variable(self):
        self.assertNotIn("wind_direction_10m", VARIABLES)

    def test_a_missing_series_does_not_disqualify_a_member(self):
        member = {"wind_speed_10m": [1.0, 2.0]}

        _attach_ungated(member, {}, "", 2)

        self.assertIsNone(member["wind_direction_10m"])
        self.assertEqual(member["wind_speed_10m"], [1.0, 2.0])

    def test_a_short_series_is_rejected_rather_than_misaligned(self):
        """A truncated series would silently pair hour 5 with hour 0."""
        member = {}

        _attach_ungated(member, {"wind_direction_10m": [90.0]}, "", 3)

        self.assertIsNone(member["wind_direction_10m"])

    def test_a_complete_series_is_carried(self):
        member = {}

        _attach_ungated(member, {"wind_direction_10m": [90.0, 180.0]}, "", 2)

        self.assertEqual(member["wind_direction_10m"], [90.0, 180.0])

    def test_every_ungated_variable_is_in_the_member_set(self):
        for name in UNGATED_VARIABLES:
            self.assertIn(name, MEMBER_VARIABLES)


class ArrowTemplateTests(TestCase):
    """
    Guard the client side. Template-source assertions as elsewhere — there is
    no JS runtime here — but the convention is worth pinning explicitly.
    """

    def setUp(self):
        self.source = TEMPLATE.read_text(encoding="utf-8")

    def test_the_arrow_is_flipped_to_the_direction_of_travel(self):
        """
        Stored direction is where the wind comes FROM; an arrow glyph points
        where it is GOING. Drop the +180 and every arrow is exactly wrong.
        """
        block = self.source.split("function arrowIcon")[1].split("return L.divIcon")[0]

        self.assertIn("+ 180", block)

    def test_the_arrows_read_the_ninth_column(self):
        """Position is the contract with map_grid_points_json."""
        self.assertIn("pt[8]", self.source)

    def test_disagreement_fades_the_arrow(self):
        self.assertIn("wa-soft", self.source)
        self.assertIn("pt[9]", self.source)

    def test_the_arrows_follow_the_timeline(self):
        """Otherwise the previous hour's arrows sit over the new contour."""
        self.assertIn("function useGridPoints", self.source)
        self.assertIn("if (windOn) renderWind();", self.source)

    def test_thinning_happens_in_projected_space(self):
        """A lat/lon stride would crowd Scotland and thin the Channel."""
        block = self.source.split("function renderWind")[1].split("function setWind")[0]

        self.assertIn("map.project(", block)

    def test_the_compass_helper_does_not_invert(self):
        """
        compassOf names the direction the wind comes from, matching how a
        forecast is spoken. Inverting here as well as in the arrow would
        double the flip.
        """
        block = self.source.split("function compassOf")[1].split("}")[0]

        self.assertNotIn("180", block)
