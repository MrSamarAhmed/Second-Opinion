"""Tests for fixture/live adapters: no-network guarantee and error mapping."""

import json
import os
import socket
import unittest
import urllib.error
from unittest import mock

from second_opinion.adapters import (
    AdapterError,
    AuthenticationError,
    EntityResolutionError,
    FixtureAdapter,
    LiveAdapter,
    MalformedResponse,
    MissingCredentials,
    UpstreamTimeout,
    describe_environment,
    name_key,
)
from second_opinion.agent import SecondOpinionAgent

from .helpers import captured_id, needs_captured, seed_entity

# Test aliases: synthetic placeholders resolved against the local captures at
# runtime, so no API-returned identifier is stored in this repository.
FONTAINES = "00000000-0000-4000-8000-0000000000a1"
RUFUS = "00000000-0000-4000-8000-0000000000c3"


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _live_adapter():
    return LiveAdapter(base_url="https://example.invalid", timeout=0.01)


class TestNameKey(unittest.TestCase):
    def test_punctuation_and_case(self):
        self.assertEqual(name_key("Fontaines_D.C."), name_key("Fontaines D.C."))
        self.assertEqual(name_key("Fontaines D.C."), "fontaines d c")

    def test_diacritics(self):
        self.assertEqual(name_key("RÜFÜS DU SOL"), "rufus du sol")

    def test_ampersand(self):
        self.assertEqual(name_key("Simon & Garfunkel"), "simon and garfunkel")


@needs_captured
class TestFixtureNoNetwork(unittest.TestCase):
    def test_fixture_mode_never_opens_a_socket(self):
        """Any socket use during a full fixture audit is a hard failure."""

        def explode(*args, **kwargs):
            raise AssertionError("fixture mode attempted a network connection")

        with mock.patch.object(socket, "socket", explode), \
             mock.patch.object(socket, "create_connection", explode):
            adapter = FixtureAdapter()
            report = SecondOpinionAgent(adapter).audit("Fontaines D.C.")
        self.assertTrue(report["ok"])
        self.assertEqual(report["mode"], "fixture")

    def test_fixture_adapter_reports_its_root_location(self):
        adapter = FixtureAdapter()
        self.assertTrue((adapter.root / "pair").is_dir())
        self.assertTrue((adapter.root / "search").is_dir())

    def test_missing_fixture_directories_raise(self):
        with self.assertRaises(AdapterError):
            FixtureAdapter(fixture_root="/nonexistent/nowhere")


@needs_captured
class TestFixtureResolution(unittest.TestCase):
    def setUp(self):
        self.adapter = FixtureAdapter()

    def test_seed_lookup_by_id_and_name(self):
        rufus = captured_id(RUFUS)
        by_id = self.adapter.resolve_seed(rufus)
        self.assertEqual(by_id.name, "RÜFÜS DU SOL")
        by_name = self.adapter.resolve_seed("rufus du sol")
        self.assertEqual(by_name.entity_id, rufus)
        dotted = self.adapter.resolve_seed("Fontaines D.C.")
        self.assertEqual(dotted.name, "Fontaines D.C.")

    def test_empty_query_raises(self):
        for query in ("", "   ", None):
            with self.assertRaises(EntityResolutionError):
                self.adapter.resolve_seed(query)

    def test_unknown_query_raises(self):
        with self.assertRaises(EntityResolutionError):
            self.adapter.resolve_seed("A Band That Does Not Exist")

    def test_seed_without_saved_candidates_raises(self):
        # A seed id that exists but has no pair file mapped to it.
        with self.assertRaises(EntityResolutionError):
            self.adapter.candidates("00000000-0000-0000-0000-000000000000")

    def test_seed_payload_falls_back_to_the_search_record(self):
        rufus = captured_id(RUFUS)
        payload = self.adapter.seed_payload(rufus)
        self.assertEqual(payload["entity_id"], rufus)
        self.assertEqual(
            len(payload.get("tags") or []), len(seed_entity("RÜFÜS DU SOL").get("tags") or [])
        )

    def test_candidates_carry_affinity_values(self):
        candidates = self.adapter.candidates(captured_id(FONTAINES))
        self.assertTrue(candidates)
        self.assertTrue(any(a is not None for _, a in candidates))


class TestLiveCredentials(unittest.TestCase):
    def test_missing_key_raises_before_any_request(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QLOO_API_KEY", None)
            with self.assertRaises(MissingCredentials):
                LiveAdapter()
            with self.assertRaises(MissingCredentials):
                LiveAdapter()

    def test_environment_report_never_contains_the_key(self):
        secret = "fake-key-value-abcdef123456"
        with mock.patch.dict(os.environ, {"QLOO_API_KEY": secret}):
            report = describe_environment()
        self.assertTrue(report["live_ready"])
        self.assertTrue(report["key_present"])
        self.assertGreater(report["key_length"], 0)
        self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(
            set(report), {"live_ready", "key_present", "key_length", "host"}
        )


class TestLiveErrorMapping(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"QLOO_API_KEY": "unit-test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_401_maps_to_authentication_error(self):
        error = urllib.error.HTTPError(
            "https://example.invalid/search", 401, "Unauthorized", {}, None
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(AuthenticationError):
                _live_adapter().resolve_seed("x")

    def test_403_maps_to_authentication_error(self):
        error = urllib.error.HTTPError(
            "https://example.invalid/search", 403, "Forbidden", {}, None
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(AuthenticationError):
                _live_adapter().resolve_seed("x")

    def test_500_maps_to_generic_adapter_error(self):
        error = urllib.error.HTTPError(
            "https://example.invalid/search", 500, "Server Error", {}, None
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(AdapterError) as ctx:
                _live_adapter().resolve_seed("x")
        self.assertNotIsInstance(ctx.exception, AuthenticationError)

    def test_bare_timeout_maps_to_upstream_timeout(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(UpstreamTimeout):
                _live_adapter().resolve_seed("x")

    def test_url_error_wrapping_timeout_maps_to_upstream_timeout(self):
        with mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError(TimeoutError())
        ):
            with self.assertRaises(UpstreamTimeout):
                _live_adapter().resolve_seed("x")

    def test_connection_error_maps_to_adapter_error(self):
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(ConnectionRefusedError("refused")),
        ):
            with self.assertRaises(AdapterError):
                _live_adapter().resolve_seed("x")

    def test_non_json_body_maps_to_malformed_response(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(b"<html>oops</html>")):
            with self.assertRaises(MalformedResponse):
                _live_adapter().resolve_seed("x")

    def test_search_with_no_results_is_an_entity_resolution_failure(self):
        body = json.dumps({"results": []}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            with self.assertRaises(EntityResolutionError):
                _live_adapter().resolve_seed("x")

    def test_search_result_without_id_is_malformed(self):
        body = json.dumps({"results": [{"name": "No Id"}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            with self.assertRaises(MalformedResponse):
                _live_adapter().resolve_seed("x")

    def test_candidates_without_entities_list_is_malformed(self):
        body = json.dumps({"results": {}}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            with self.assertRaises(MalformedResponse):
                _live_adapter().candidates("some-id")

    def test_live_mode_cannot_start_without_a_key(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QLOO_API_KEY", None)
            with self.assertRaises(MissingCredentials) as ctx:
                agent = SecondOpinionAgent(LiveAdapter())
        self.assertIn("QLOO_API_KEY", str(ctx.exception))
        self.assertNotIn("unit-test-key", str(ctx.exception))

    def test_search_uses_the_documented_types_parameter(self):
        """A singular `type` is silently ignored by /search, so assert `types`."""
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            return _FakeResponse(json.dumps({"results": [{"entity_id": "X"}]}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _live_adapter().resolve_seed("Fontaines D.C.")

        parsed = urllib.parse.urlparse(captured["url"])
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/search")
        self.assertEqual(query["types"], ["urn:entity:artist"])
        self.assertNotIn("type", query)

    def test_search_404_is_treated_as_no_results(self):
        error = urllib.error.HTTPError(
            "https://example.invalid/search", 404, "No results", {}, None
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(EntityResolutionError) as ctx:
                _live_adapter().resolve_seed("x")
        self.assertEqual(type(ctx.exception), EntityResolutionError)

    def test_socket_timeout_wrapped_in_url_error_maps_to_upstream_timeout(self):
        import socket as socket_module

        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(socket_module.timeout("timed out")),
        ):
            with self.assertRaises(UpstreamTimeout):
                _live_adapter().resolve_seed("x")

    def test_seed_payload_requests_the_entity_by_filter_not_by_signal(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            return _FakeResponse(
                json.dumps({"results": {"entities": [{"entity_id": "SEED", "name": "S"}]}}).encode()
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            record = _live_adapter().seed_payload("SEED")

        self.assertEqual(record["entity_id"], "SEED")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(captured["url"]).query)
        self.assertEqual(query["filter.results.entities"], ["SEED"])
        self.assertNotIn("signal.interests.entities", query)

    def test_seed_payload_rejects_a_mismatched_entity(self):
        body = json.dumps({"results": {"entities": [{"entity_id": "OTHER"}]}}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            with self.assertRaises(EntityResolutionError):
                _live_adapter().seed_payload("SEED")

    def test_seed_payload_with_empty_results_is_a_resolution_failure(self):
        body = json.dumps({"results": {"entities": []}}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            with self.assertRaises(EntityResolutionError):
                _live_adapter().seed_payload("SEED")

    def test_request_carries_the_key_in_a_header_not_the_url(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            return _FakeResponse(json.dumps({"results": [{"entity_id": "X"}]}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _live_adapter().resolve_seed("seed name")

        self.assertNotIn("key", captured["url"].lower())
        header_names = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(header_names["x-api-key"], "unit-test-key")


if __name__ == "__main__":
    unittest.main()
