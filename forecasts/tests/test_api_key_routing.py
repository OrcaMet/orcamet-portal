"""
The paid API key has to reach a host that honours it.

Open-Meteo serves keyed traffic from a separate host - "The server URL
requires the prefix customer-". Sending a key to the free host is not an
error; the key is ignored and the request is served as anonymous traffic on
free-tier limits.

That is exactly how this went unnoticed for weeks. `risk_grid` read
OPENMETEO_API_KEY, printed "API key: ****abcd" on every run, and then called
ensemble-api.open-meteo.com without the key in the query at all - because
ensemble.py never read the setting. A live run lost 140 of 380 points to 429s
and still reported success:

    Complete: 17280 records (240 points, 140 skipped) in 3210s
    using 24 API calls

Every lost point was north of 55.9N, so the map simply stopped at the
Scottish border.
"""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from forecasts.engine import ensemble as ens
from forecasts.engine.core import (
    MODELS_CONFIG, customer_url, openmeteo_host, scrub_key,
)

KEY = "test-key-not-real"


class CustomerUrlTests(SimpleTestCase):

    def test_no_key_leaves_the_free_host_alone(self):
        """An unkeyed deployment must behave exactly as it did before."""
        self.assertEqual(
            customer_url("https://api.open-meteo.com/v1/forecast", ""),
            "https://api.open-meteo.com/v1/forecast",
        )

    def test_a_key_moves_the_request_to_the_customer_host(self):
        self.assertEqual(
            customer_url("https://api.open-meteo.com/v1/forecast", KEY),
            "https://customer-api.open-meteo.com/v1/forecast",
        )

    def test_the_ensemble_host_gets_the_same_prefix(self):
        """
        The prefix goes on the host, not in front of the whole domain:
        ensemble-api -> customer-ensemble-api.
        """
        self.assertEqual(
            customer_url("https://ensemble-api.open-meteo.com/v1/ensemble", KEY),
            "https://customer-ensemble-api.open-meteo.com/v1/ensemble",
        )

    def test_the_path_and_scheme_survive(self):
        url = customer_url("https://api.open-meteo.com/v1/dwd-icon", KEY)

        self.assertTrue(url.startswith("https://"))
        self.assertTrue(url.endswith("/v1/dwd-icon"))

    def test_rewriting_is_idempotent(self):
        once = customer_url("https://api.open-meteo.com/v1/forecast", KEY)

        self.assertEqual(customer_url(once, KEY), once)

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_the_key_is_read_from_settings_when_not_passed(self):
        self.assertEqual(
            customer_url("https://api.open-meteo.com/v1/forecast"),
            "https://customer-api.open-meteo.com/v1/forecast",
        )


class EnsembleRequestTests(SimpleTestCase):
    """
    The regression, at the request layer.

    Only the session is replaced, so the real functions build the real URL
    and the real query.
    """

    def setUp(self):
        # The downgrade latch is module state; reset it per test.
        ens._ensemble_keyed = True
        self.addCleanup(setattr, ens, "_ensemble_keyed", True)

    def _capture(self, fn):
        seen = {}

        class Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"hourly": {}}

        def get(url, params=None, timeout=None):
            seen["url"] = url
            seen["params"] = params or {}
            return Resp()

        with patch("forecasts.engine.ensemble._session.get", side_effect=get):
            try:
                fn()
            except (ens.EnsembleUnavailable, ValueError, KeyError,
                    TypeError, IndexError):
                # An empty payload is fine; the request is what is on trial.
                # Anything else - a missing function, say - must still fail.
                pass

        self.assertIn("url", seen, "no request was made")
        return seen

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_the_grid_fetch_sends_the_key(self):
        seen = self._capture(
            lambda: ens.fetch_grid_members([55.9], [-3.1], forecast_days=1)
        )

        self.assertEqual(seen["params"].get("apikey"), KEY)

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_the_grid_fetch_uses_the_customer_host(self):
        seen = self._capture(
            lambda: ens.fetch_grid_members([55.9], [-3.1], forecast_days=1)
        )

        self.assertIn("customer-ensemble-api.open-meteo.com", seen["url"])

    @override_settings(OPENMETEO_API_KEY="")
    def test_without_a_key_nothing_changes(self):
        seen = self._capture(
            lambda: ens.fetch_grid_members([55.9], [-3.1], forecast_days=1)
        )

        self.assertNotIn("apikey", seen["params"])
        self.assertEqual(seen["url"], ens.ENSEMBLE_HOST)

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_the_per_point_fetch_sends_the_key_too(self):
        """Both entry points were unkeyed, so both are covered."""
        seen = self._capture(
            lambda: ens.fetch_members(
                55.9, -3.1, forecast_days=1, models=("ecmwf_ifs025_ensemble",)
            )
        )

        self.assertEqual(seen["params"].get("apikey"), KEY)
        self.assertIn("customer-ensemble-api.open-meteo.com", seen["url"])

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_the_reported_endpoint_matches_the_one_used(self):
        """
        risk_grid prints this. It printed the base host before, which is why
        a key that was never sent still looked configured.
        """
        seen = self._capture(
            lambda: ens.fetch_grid_members([55.9], [-3.1], forecast_days=1)
        )

        self.assertEqual(ens.ensemble_url(), seen["url"])


class SiteForecastRequestTests(SimpleTestCase):
    """The per-site path sent the key, but to the host that ignores it."""

    def _capture(self):
        seen = {}

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"hourly": {}}

        def get(url, params=None, timeout=None):
            seen["url"] = url
            seen["params"] = params or {}
            return Resp()

        from forecasts.engine import core

        with patch("forecasts.engine.core._session.get", side_effect=get):
            try:
                core.fetch_single_model(
                    "ukv", 55.9, -3.1, "2026-09-02", "2026-09-03"
                )
            except (ValueError, KeyError, TypeError, IndexError):
                pass

        self.assertIn("url", seen, "no request was made")
        return seen

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_it_now_uses_the_customer_host(self):
        seen = self._capture()

        self.assertIn("customer-api.open-meteo.com", seen["url"])
        self.assertEqual(seen["params"].get("apikey"), KEY)

    @override_settings(OPENMETEO_API_KEY="")
    def test_unkeyed_it_stays_on_the_free_host(self):
        seen = self._capture()

        self.assertEqual(seen["url"], MODELS_CONFIG["ukv"]["url"])
        self.assertNotIn("apikey", seen["params"])

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_the_diagnostic_host_follows_the_key(self):
        self.assertEqual(openmeteo_host(), "https://customer-api.open-meteo.com")


class KeyRedactionTests(SimpleTestCase):
    """
    Putting the key in the query string put it on a path to the logs.

    requests embeds the full URL in its exception text, and this code both
    logs those and stores them in UKRiskGridRun.error_message - so a single
    429 would have written the credential to Render's log stream and to the
    database.
    """

    def test_the_key_is_redacted_from_a_real_error_string(self):
        err = (
            "HTTPSConnectionPool(host='customer-ensemble-api.open-meteo.com', "
            "port=443): Max retries exceeded with url: /v1/ensemble?"
            "latitude=57.9000&apikey=" + KEY + "&models=ecmwf_ifs025_ensemble "
            "(Caused by ResponseError('too many 429 error responses'))"
        )

        cleaned = scrub_key(err)

        self.assertNotIn(KEY, cleaned)
        self.assertIn("apikey=***", cleaned)

    def test_the_rest_of_the_message_survives(self):
        """Redaction must not cost us the diagnostic."""
        cleaned = scrub_key("failed: /v1/ensemble?apikey=" + KEY + "&models=x")

        self.assertIn("/v1/ensemble", cleaned)
        self.assertIn("models=x", cleaned)

    def test_it_stops_at_the_parameter_boundary(self):
        """Only the value goes, not everything after it."""
        cleaned = scrub_key("?apikey=" + KEY + "&latitude=55.9")

        self.assertIn("latitude=55.9", cleaned)

    def test_a_trailing_key_is_still_redacted(self):
        cleaned = scrub_key("?latitude=55.9&apikey=" + KEY)

        self.assertNotIn(KEY, cleaned)

    def test_messages_without_a_key_are_untouched(self):
        msg = "Ensemble probe failed: connection reset"

        self.assertEqual(scrub_key(msg), msg)

    def test_it_accepts_an_exception_object(self):
        exc = RuntimeError("boom apikey=" + KEY)

        self.assertNotIn(KEY, scrub_key(exc))


class EnsembleKeyRefusedTests(SimpleTestCase):
    """
    Open-Meteo licenses the Ensemble API separately from the standard one.

    Our key is accepted by customer-api.open-meteo.com - a live run fetched
    72 hours from all four deterministic models - and refused by
    customer-ensemble-api.open-meteo.com with:

        403 Client Error: Forbidden for url: .../v1/ensemble?...&apikey=***

    A 403 is not a rate limit, so none of the retry machinery touches it.
    Keyed-only behaviour would have taken the grid from a degraded 240
    points to zero.
    """

    def setUp(self):
        ens._ensemble_keyed = True
        self.addCleanup(setattr, ens, "_ensemble_keyed", True)

    def _api(self, first_status):
        """Refuse the customer host, serve the free one."""
        calls = []

        class Resp:
            def __init__(self, status):
                self.status_code = status

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise AssertionError(
                        f"raise_for_status called on {self.status_code}"
                    )

            def json(self):
                return {"hourly": {}}

        def get(url, params=None, timeout=None):
            calls.append((url, params or {}))
            if "customer-" in url:
                return Resp(first_status)
            return Resp(200)

        return calls, get

    def _run(self, calls, get):
        with patch("forecasts.engine.ensemble._session.get", side_effect=get):
            try:
                ens.fetch_grid_members([55.9], [-3.1], forecast_days=1)
            except (ens.EnsembleUnavailable, ValueError, KeyError,
                    TypeError, IndexError):
                pass

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_a_403_falls_back_to_the_free_host(self):
        calls, get = self._api(403)

        self._run(calls, get)

        self.assertEqual(len(calls), 2, "no fallback attempt was made")
        self.assertIn("customer-ensemble-api", calls[0][0])
        self.assertEqual(calls[1][0], ens.ENSEMBLE_HOST)

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_the_fallback_request_carries_no_key(self):
        """The free host must not be handed a credential it cannot use."""
        calls, get = self._api(403)

        self._run(calls, get)

        self.assertNotIn("apikey", calls[1][1])

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_a_401_falls_back_too(self):
        calls, get = self._api(401)

        self._run(calls, get)

        self.assertEqual(calls[1][0], ens.ENSEMBLE_HOST)

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_the_downgrade_latches(self):
        """
        38 batches must not each pay for a rejected request.

        After the first refusal every later call goes straight to the free
        host.
        """
        calls, get = self._api(403)

        self._run(calls, get)
        self._run(calls, get)
        self._run(calls, get)

        customer = [c for c in calls if "customer-" in c[0]]
        self.assertEqual(len(customer), 1, "the key was retried after refusal")

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_the_reported_endpoint_follows_the_downgrade(self):
        calls, get = self._api(403)

        self.assertIn("customer-", ens.ensemble_url())
        self._run(calls, get)
        self.assertEqual(ens.ensemble_url(), ens.ENSEMBLE_HOST)

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_an_accepted_key_is_not_downgraded(self):
        """If the plan does cover ensembles, nothing changes."""
        calls, get = self._api(200)

        self._run(calls, get)

        self.assertEqual(len(calls), 1)
        self.assertIn("customer-ensemble-api", calls[0][0])
        self.assertEqual(calls[0][1].get("apikey"), KEY)
        self.assertTrue(ens.ensemble_keyed())

    @override_settings(OPENMETEO_API_KEY=KEY)
    def test_a_500_is_not_treated_as_a_refusal(self):
        """A server error must surface, not silently drop the key."""
        calls, get = self._api(500)

        with patch("forecasts.engine.ensemble._session.get", side_effect=get):
            with self.assertRaises(AssertionError):
                ens.fetch_grid_members([55.9], [-3.1], forecast_days=1)

        self.assertTrue(ens._ensemble_keyed, "a 500 downgraded the key")
