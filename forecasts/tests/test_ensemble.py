"""
Tests for ensemble-derived probability of cancellation.

Members are synthesised so the expected answer is exact and no network is
touched. The property under test is the one the whole redesign rests on: a
wider spread below a limit can be more dangerous than a tighter one above it.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from django.test import TestCase

from forecasts.engine import ensemble

TH = {
    "wind_mean_caution": 10.0, "wind_mean_cancel": 14.0,
    "gust_caution": 15.0, "gust_cancel": 20.0,
    "precip_caution": 0.7, "precip_cancel": 2.0,
    "temp_min_caution": 1.0, "temp_min_cancel": -2.0,
    "temp_max_caution": 27.0, "temp_max_cancel": 32.0,
}


def make_times(day=date(2026, 1, 15), hours=range(24)):
    """UTC hours for one day. January, so local time is GMT — no offset."""
    return [
        datetime(day.year, day.month, day.day, h, tzinfo=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M")
        for h in hours
    ]


def make_members(gusts, n_hours=24, wind=2.0, precip=0.0, temp=10.0):
    """One member per gust value; that gust is held all day."""
    return [
        {
            "wind_speed_10m": [wind] * n_hours,
            "wind_gusts_10m": [g] * n_hours,
            "precipitation": [precip] * n_hours,
            "temperature_2m": [temp] * n_hours,
        }
        for g in gusts
    ]


class CancellationProbabilityTests(TestCase):
    def _p(self, gusts, **kw):
        times = make_times()
        members = make_members(gusts, **kw)
        out = ensemble.cancellation_probability(times, members, TH)
        return out[date(2026, 1, 15)]

    def test_no_member_breaching_gives_zero(self):
        self.assertEqual(self._p([5.0] * 10)["p_cancel"], 0.0)

    def test_every_member_breaching_gives_one(self):
        self.assertEqual(self._p([25.0] * 10)["p_cancel"], 1.0)

    def test_half_the_members_give_a_half(self):
        self.assertEqual(self._p([25.0] * 5 + [5.0] * 5)["p_cancel"], 0.5)

    def test_member_count_is_reported(self):
        self.assertEqual(self._p([5.0] * 7)["members"], 7)

    def test_a_member_breaching_all_day_still_counts_once(self):
        """The question is whether the scenario stops the job, not how often."""
        self.assertEqual(self._p([25.0] + [5.0] * 3)["p_cancel"], 0.25)

    def test_caution_is_tracked_separately(self):
        out = self._p([16.0] * 4)          # over caution, under cancel
        self.assertEqual(out["p_cancel"], 0.0)
        self.assertEqual(out["p_caution"], 1.0)

    def test_breakdown_names_the_limit_that_was_hit(self):
        out = self._p([25.0] * 4)
        self.assertEqual(out["by_variable"], {"gust_cancel": 1.0})

    def test_breakdown_covers_several_causes(self):
        times = make_times()
        members = (
            make_members([25.0, 25.0])                      # gusts
            + make_members([5.0], precip=5.0)               # rain
            + make_members([5.0])                           # benign
        )
        out = ensemble.cancellation_probability(times, members, TH)[date(2026, 1, 15)]

        self.assertAlmostEqual(out["p_cancel"], 0.75)
        self.assertAlmostEqual(out["by_variable"]["gust_cancel"], 0.5)
        self.assertAlmostEqual(out["by_variable"]["precip_cancel"], 0.25)

    def test_temperature_cancels_at_both_ends(self):
        times = make_times()
        cold = ensemble.cancellation_probability(
            times, make_members([5.0], temp=-5.0), TH)[date(2026, 1, 15)]
        hot = ensemble.cancellation_probability(
            times, make_members([5.0], temp=40.0), TH)[date(2026, 1, 15)]

        self.assertEqual(cold["p_cancel"], 1.0)
        self.assertEqual(hot["p_cancel"], 1.0)
        self.assertIn("temperature", cold["by_variable"])

    def test_blank_heat_thresholds_disable_the_heat_gate(self):
        th = dict(TH, temp_max_caution=None, temp_max_cancel=None)
        times = make_times()
        out = ensemble.cancellation_probability(
            times, make_members([5.0], temp=40.0), th)[date(2026, 1, 15)]
        self.assertEqual(out["p_cancel"], 0.0)


class SpreadBeatsMeanTests(TestCase):
    """The premise of the redesign, stated as a test."""

    def _p(self, gusts):
        times = make_times()
        out = ensemble.cancellation_probability(times, make_members(gusts), TH)
        return out[date(2026, 1, 15)]["p_cancel"]

    def test_wider_spread_below_the_limit_beats_a_tighter_one_nearer_it(self):
        tight_high = [18.0, 18.5, 19.0, 19.0, 19.5]        # mean 18.8, none breach
        wide_low = [8.0, 12.0, 16.0, 24.0, 28.0]           # mean 17.6, two breach

        self.assertLess(sum(wide_low) / 5, sum(tight_high) / 5)
        self.assertEqual(self._p(tight_high), 0.0)
        self.assertEqual(self._p(wide_low), 0.4)

    def test_agreement_just_over_the_limit_is_certain(self):
        self.assertEqual(self._p([20.1] * 5), 1.0)


class WorkWindowTests(TestCase):
    def test_only_work_hours_are_considered(self):
        """A gale at 03:00 does not cancel a job that starts at 07:00."""
        times = make_times()
        member = {
            "wind_speed_10m": [2.0] * 24,
            "wind_gusts_10m": [40.0 if h == 3 else 4.0 for h in range(24)],
            "precipitation": [0.0] * 24,
            "temperature_2m": [10.0] * 24,
        }
        out = ensemble.cancellation_probability(times, [member], TH)
        self.assertEqual(out[date(2026, 1, 15)]["p_cancel"], 0.0)

    def test_a_breach_inside_the_window_counts(self):
        times = make_times()
        member = {
            "wind_speed_10m": [2.0] * 24,
            "wind_gusts_10m": [40.0 if h == 10 else 4.0 for h in range(24)],
            "precipitation": [0.0] * 24,
            "temperature_2m": [10.0] * 24,
        }
        out = ensemble.cancellation_probability(times, [member], TH)
        self.assertEqual(out[date(2026, 1, 15)]["p_cancel"], 1.0)

    def test_window_is_applied_in_local_time(self):
        """
        1 July is BST. 06:00 UTC is 07:00 local — inside the window — while
        18:00 UTC is 19:00 local, outside it.
        """
        times = make_times(day=date(2026, 7, 1))
        early = {
            "wind_speed_10m": [2.0] * 24,
            "wind_gusts_10m": [40.0 if h == 6 else 4.0 for h in range(24)],
            "precipitation": [0.0] * 24,
            "temperature_2m": [10.0] * 24,
        }
        late = {
            "wind_speed_10m": [2.0] * 24,
            "wind_gusts_10m": [40.0 if h == 18 else 4.0 for h in range(24)],
            "precipitation": [0.0] * 24,
            "temperature_2m": [10.0] * 24,
        }

        got_early = ensemble.cancellation_probability(times, [early], TH)
        got_late = ensemble.cancellation_probability(times, [late], TH)

        self.assertEqual(got_early[date(2026, 7, 1)]["p_cancel"], 1.0)
        self.assertEqual(got_late[date(2026, 7, 1)]["p_cancel"], 0.0)


class PercentileTests(TestCase):
    def test_percentiles_describe_the_distribution(self):
        times = make_times(hours=range(1))
        members = make_members([float(v) for v in range(1, 101)], n_hours=1)

        p = ensemble.hourly_percentiles(times, members, "wind_gusts_10m")[0]

        self.assertAlmostEqual(p["p50"], 50.5)
        self.assertLess(p["p10"], p["p50"])
        self.assertGreater(p["p90"], p["p50"])

    def test_missing_values_are_skipped(self):
        times = make_times(hours=range(1))
        members = make_members([5.0], n_hours=1)
        members[0]["wind_gusts_10m"] = [None]

        self.assertIsNone(
            ensemble.hourly_percentiles(times, members, "wind_gusts_10m")[0]
        )


class FetchMembersTests(TestCase):
    def _response(self, members=3, hours=2, time_offset=0):
        times = [f"2026-01-15T{h + time_offset:02d}:00" for h in range(hours)]
        hourly = {"time": times}
        for i in range(1, members + 1):
            for var in ensemble.VARIABLES:
                hourly[f"{var}_member{i:02d}"] = [1.0] * hours
        return {"hourly": hourly}

    def test_members_are_pooled_across_ensembles(self):
        class Resp:
            status_code = 200
            def __init__(self, payload): self._p = payload
            def raise_for_status(self): pass
            def json(self): return self._p

        payload = self._response(members=3)
        with patch.object(ensemble._session, "get", return_value=Resp(payload)):
            times, members = ensemble.fetch_members(55.0, -3.0)

        # Three ensembles configured, three members each.
        self.assertEqual(len(members), 9)
        self.assertEqual(len(times), 2)

    def test_an_ensemble_on_a_different_time_axis_is_excluded(self):
        """Pooling depends on a shared axis; misaligned members are dropped."""
        class Resp:
            status_code = 200
            def __init__(self, payload): self._p = payload
            def raise_for_status(self): pass
            def json(self): return self._p

        good = Resp(self._response(members=2))
        shifted = Resp(self._response(members=2, time_offset=5))

        with patch.object(ensemble._session, "get",
                          side_effect=[good, shifted, shifted]):
            _times, members = ensemble.fetch_members(55.0, -3.0)

        self.assertEqual(len(members), 2)

    def test_total_failure_raises(self):
        with patch.object(ensemble._session, "get", side_effect=OSError("down")):
            with self.assertRaises(ensemble.EnsembleUnavailable):
                ensemble.fetch_members(55.0, -3.0)
