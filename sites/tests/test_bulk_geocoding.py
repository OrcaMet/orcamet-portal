"""
Tests for bulk postcode geocoding.

The single-postcode path, and the form validation built on it, are covered
separately in test_geocoding.py.

What matters here is the contract the importer relies on: every input
postcode is a key in the result, normalisation matches postcodes.io's echoed
formatting back to what the user typed, and a failed chunk degrades to
unresolved rows rather than taking the import down.
"""

from unittest.mock import patch

from django.test import TestCase

from sites.models import (
    BULK_GEOCODE_CHUNK, geocode_postcodes_bulk, normalise_postcode,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def response_for(request_postcodes, known):
    """Build a postcodes.io-shaped reply: one entry per query, result or null."""
    return FakeResponse({
        "status": 200,
        "result": [
            {
                "query": code,
                # postcodes.io echoes its own spacing, not the caller's.
                "result": (
                    {"latitude": known[code][0], "longitude": known[code][1]}
                    if code in known else None
                ),
            }
            for code in request_postcodes
        ],
    })


class NormalisePostcodeTests(TestCase):
    def test_strips_spaces_and_uppercases(self):
        for variant in ("eh1 1yz", "EH11YZ", " Eh1  1Yz "):
            self.assertEqual(normalise_postcode(variant), "EH11YZ")

    def test_handles_empty_and_none(self):
        self.assertEqual(normalise_postcode(""), "")
        self.assertEqual(normalise_postcode(None), "")


class GeocodeBulkTests(TestCase):
    KNOWN = {"EH11YZ": (55.951, -3.189), "AB115DQ": (57.143, -2.081)}

    def test_resolves_known_postcodes(self):
        with patch("requests.post") as post:
            post.return_value = response_for(["EH11YZ", "AB115DQ"], self.KNOWN)
            result = geocode_postcodes_bulk(["eh1 1yz", "AB11 5DQ"])

        self.assertEqual(result["EH11YZ"], (55.951, -3.189))
        self.assertEqual(result["AB115DQ"], (57.143, -2.081))

    def test_unknown_postcode_comes_back_as_a_key_with_no_coordinates(self):
        """
        The importer reads coords[key] for every row, so a missing key would
        be an unexplained row rather than 'postcode not found'.
        """
        with patch("requests.post") as post:
            post.return_value = response_for(["EH11YZ", "XX999ZZ"], self.KNOWN)
            result = geocode_postcodes_bulk(["EH1 1YZ", "XX99 9ZZ"])

        self.assertIn("XX999ZZ", result)
        self.assertEqual(result["XX999ZZ"], (None, None))

    def test_postcodes_io_spacing_still_matches_the_input(self):
        """It echoes 'EH1 1YZ' for a query of 'EH11YZ'; both must key the same."""
        with patch("requests.post") as post:
            post.return_value = FakeResponse({
                "status": 200,
                "result": [{
                    "query": "EH1 1YZ",
                    "result": {"latitude": 55.951, "longitude": -3.189},
                }],
            })
            result = geocode_postcodes_bulk(["eh11yz"])

        self.assertEqual(result["EH11YZ"], (55.951, -3.189))

    def test_duplicates_are_looked_up_once(self):
        with patch("requests.post") as post:
            post.return_value = response_for(["EH11YZ"], self.KNOWN)
            geocode_postcodes_bulk(["EH1 1YZ", "eh1 1yz", "EH11YZ"])

        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["postcodes"], ["EH11YZ"])

    def test_chunks_at_the_api_limit(self):
        count = BULK_GEOCODE_CHUNK * 2 + 5
        postcodes = [f"EH{n}" for n in range(count)]

        with patch("requests.post") as post:
            post.return_value = FakeResponse({"status": 200, "result": []})
            result = geocode_postcodes_bulk(postcodes)

        self.assertEqual(post.call_count, 3)
        for call in post.call_args_list:
            self.assertLessEqual(
                len(call.kwargs["json"]["postcodes"]), BULK_GEOCODE_CHUNK
            )
        self.assertEqual(len(result), count)

    def test_a_failed_chunk_leaves_its_rows_unresolved(self):
        """
        One bad chunk must not lose the whole import — the rows it covers
        come back as 'not found' and the user can retry them.
        """
        postcodes = [f"EH{n}" for n in range(BULK_GEOCODE_CHUNK + 1)]

        with patch("requests.post") as post:
            post.side_effect = [
                RuntimeError("connection reset"),
                FakeResponse({
                    "status": 200,
                    "result": [{
                        "query": postcodes[-1],
                        "result": {"latitude": 1.0, "longitude": 2.0},
                    }],
                }),
            ]
            result = geocode_postcodes_bulk(postcodes)

        self.assertEqual(result[normalise_postcode(postcodes[0])], (None, None))
        self.assertEqual(result[normalise_postcode(postcodes[-1])], (1.0, 2.0))

    def test_a_non_200_body_does_not_raise(self):
        with patch("requests.post") as post:
            post.return_value = FakeResponse({"status": 500, "result": None})
            result = geocode_postcodes_bulk(["EH1 1YZ"])

        self.assertEqual(result["EH11YZ"], (None, None))

    def test_empty_input_makes_no_request(self):
        with patch("requests.post") as post:
            self.assertEqual(geocode_postcodes_bulk([]), {})
            self.assertEqual(geocode_postcodes_bulk(["", "  "]), {})
        post.assert_not_called()
