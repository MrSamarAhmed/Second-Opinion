"""HTTP-layer tests: routes, the static seed catalogue, mode handling, and key non-disclosure."""

import json
import os
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from second_opinion.adapters import (
    AdapterError,
    AuthenticationError,
    EntityResolutionError,
    MissingCredentials,
    UpstreamTimeout,
)
from second_opinion.catalog import SEED_CATALOG
from second_opinion.server import Handler
from second_opinion.throttle import AuditThrottle, set_default_throttle

from .helpers import needs_captured

# A public artist name from the hand-authored seed catalogue, URL-quoted. Tests
# never use an API-returned entity id.
SEED_QUERY = urllib.parse.quote("Fontaines D.C.")
SECRET = "super-secret-key-that-must-never-leak"


class _QuietHandler(Handler):
    def log_message(self, fmt, *args):
        pass


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=20) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        # These tests assert routing, payloads and status codes, not the limiter.
        # A generous budget keeps them independent of how many audits a method
        # makes; the limit itself is covered in test_rate_limit.py.
        set_default_throttle(AuditThrottle(per_minute=1000, burst=1000, concurrent=8))
        self.addCleanup(set_default_throttle, None)

    def json_get(self, path):
        status, headers, body = _get(self.port, path)
        return status, headers, json.loads(body)


class TestBasicRoutes(ServerTestCase):
    """No captured fixtures required: the public surface must work on a clean checkout."""

    def test_health(self):
        status, _, payload = self.json_get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True})

    def test_rules_are_published(self):
        status, _, payload = self.json_get("/api/rules")
        self.assertEqual(status, 200)
        self.assertEqual(payload["thresholds"]["strong_threshold"], 0.30)
        self.assertEqual(set(payload["states"]), {"CORROBORATED", "AFFINITY_ONLY", "UNVERIFIABLE"})

    def test_seeds_are_the_hand_authored_catalog(self):
        status, _, payload = self.json_get("/api/seeds")
        self.assertEqual(status, 200)
        self.assertEqual(payload["catalog"], "static")
        self.assertEqual(payload["mode"], "live")
        self.assertEqual(
            [s["name"] for s in payload["seeds"]], [name for name, _ in SEED_CATALOG]
        )
        self.assertIn("live_ready", payload["environment"])

    def test_seed_payload_carries_no_captured_data(self):
        _, _, payload = self.json_get("/api/seeds")
        for seed in payload["seeds"]:
            self.assertEqual(set(seed), {"name", "label", "note"})
            for value in seed.values():
                self.assertIsInstance(value, str)
        body = json.dumps(payload["seeds"])
        # No entity ids, affinities or tag counts leak into the public seed list.
        self.assertNotIn("entity_id", body)
        self.assertNotIn("affinity", body)
        self.assertNotIn("tags", body)

    def test_seed_list_does_not_need_fixture_files(self):
        class ExplodingFixture:
            def __init__(self, *a, **k):
                raise AdapterError("fixture directories not found")

        with mock.patch("second_opinion.server.FixtureAdapter", ExplodingFixture):
            status, _, payload = self.json_get("/api/seeds")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["seeds"]), len(SEED_CATALOG))

    def test_index_is_served(self):
        status, headers, body = _get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("text/html"))
        self.assertIn(b"Second Opinion", body)

    def test_index_does_not_reference_captured_payloads(self):
        _, _, body = _get(self.port, "/")
        text = body.decode("utf-8")
        self.assertNotIn("data/fixtures", text)
        self.assertNotIn("entity_id", text)

    def test_unknown_route_is_404(self):
        status, _, payload = self.json_get("/api/nope")
        self.assertEqual(status, 404)

    def test_static_traversal_is_rejected(self):
        status, _, _ = _get(self.port, "/static/../../second_opinion/config.py")
        self.assertEqual(status, 400)

    def test_unknown_mode_is_400(self):
        status, _, payload = self.json_get("/api/audit?seed=Whatever&mode=sneaky")
        self.assertEqual(status, 400)
        self.assertIn("unknown mode", payload["error"])


class TestLiveModeWithoutCredentials(ServerTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("QLOO_API_KEY", None)

    def test_live_mode_is_the_default_and_reports_no_key(self):
        _, _, payload = self.json_get("/api/seeds")
        self.assertEqual(payload["mode"], "live")
        self.assertFalse(payload["environment"]["live_ready"])

    def test_live_audit_without_key_is_503_and_names_the_variable(self):
        status, _, payload = self.json_get(f"/api/audit?seed={SEED_QUERY}&mode=live")
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["mode"], "live")
        self.assertIn("QLOO_API_KEY", payload["error"])

    def test_default_mode_does_not_fall_back_to_fixtures(self):
        # No explicit mode and no key: the server must refuse, not read fixtures.
        status, _, payload = self.json_get("/api/audit?seed=Fontaines%20D.C.")
        self.assertEqual(status, 503)
        self.assertEqual(payload["mode"], "live")

    def test_so_mode_fixture_switches_the_server(self):
        with mock.patch.dict(os.environ, {"SO_MODE": "fixture"}, clear=False):
            _, _, payload = self.json_get("/api/seeds")
        self.assertEqual(payload["mode"], "fixture")

    def test_secret_key_never_appears_in_any_response(self):
        with mock.patch.dict(os.environ, {"QLOO_API_KEY": SECRET}):
            bodies = []
            for path in ("/api/health", "/api/rules", "/api/seeds"):
                status, _, body = _get(self.port, path)
                bodies.append(body.decode("utf-8", "replace"))
                self.assertNotIn(SECRET, bodies[-1], path)
            seeds = json.loads(bodies[2])
        self.assertTrue(seeds["environment"]["live_ready"])
        self.assertEqual(set(seeds["environment"]), {"live_ready", "host"})


@needs_captured
class TestFixtureModeAudit(ServerTestCase):
    def test_fixture_audit_runs_when_requested_explicitly(self):
        status, headers, payload = self.json_get(
            f"/api/audit?seed={SEED_QUERY}&mode=fixture"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "fixture")
        self.assertTrue(payload["candidates"])
        self.assertTrue(all(c["receipts_verified"] for c in payload["candidates"]))

    def test_refusal_is_a_normal_200_with_ok_false(self):
        seed = urllib.parse.quote("RÜFÜS DU SOL")
        status, _, payload = self.json_get(f"/api/audit?seed={seed}&mode=fixture")
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["refusal"]["code"], "insufficient seed metadata")

    def test_unknown_seed_refuses(self):
        status, _, payload = self.json_get("/api/audit?seed=NotARealArtist&mode=fixture")
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["refusal"]["code"], "seed resolution failed")

    def test_bad_threshold_is_400(self):
        status, _, payload = self.json_get(
            f"/api/audit?seed={SEED_QUERY}&mode=fixture&threshold=abc"
        )
        self.assertEqual(status, 400)

    def test_threshold_override_is_honoured(self):
        _, _, payload = self.json_get(
            f"/api/audit?seed={SEED_QUERY}&mode=fixture&threshold=0.45"
        )
        self.assertEqual(payload["rules"]["thresholds"]["strong_threshold"], 0.45)


class TestMissingSeed(ServerTestCase):
    def test_missing_seed_is_400(self):
        status, _, payload = self.json_get("/api/audit?mode=live")
        self.assertEqual(status, 400)
        self.assertIn("seed", payload["error"])


class _StubLiveAdapter:
    """Stand-in for LiveAdapter: resolves names, serves fixed payloads, no network."""

    mode = "live"

    def __init__(self, *args, **kwargs):
        self.resolved = []

    def resolve_seed(self, query):
        from second_opinion.adapters import SeedRef

        if query.lower() == "nowhere band":
            raise EntityResolutionError("Qloo returned no entity for 'Nowhere Band'")
        self.resolved.append(query)
        return SeedRef("stub-seed-id", query)

    def seed_payload(self, seed_id):
        return {
            "entity_id": seed_id,
            "name": "Stub Seed",
            "properties": {
                "primary_genres": ["post-punk"],
                "lyrical_themes": ["melancholy"],
            },
            "tags": [
                {"id": "urn:tag:audience:qloo", "name": "students", "type": "urn:tag:audience:qloo"},
                {"id": "urn:tag:style:qloo", "name": "raw production", "type": "urn:tag:style:qloo"},
            ],
        }

    def candidates(self, seed_id):
        return [(
            {
                "entity_id": "stub-cand-id",
                "name": "Stub Candidate",
                "properties": {
                    "primary_genres": ["Post-Punk"],
                    "lyrical_themes": ["melancholy"],
                },
                "tags": [
                    {"id": "urn:tag:audience:qloo", "name": "students", "type": "urn:tag:audience:qloo"},
                    {"id": "urn:tag:style:qloo", "name": "raw production", "type": "urn:tag:style:qloo"},
                ],
                "query": {"affinity": 0.81},
            },
            0.81,
        )]


class TestLiveSeedFlow(ServerTestCase):
    """Selecting a seed resolves it live, then audits the live recommendation set."""

    def test_live_audit_uses_the_adapter_for_the_clicked_name(self):
        with mock.patch("second_opinion.server.LiveAdapter", _StubLiveAdapter):
            status, _, payload = self.json_get(
                "/api/audit?seed=Laufey&mode=live"
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "live")
        self.assertEqual(payload["seed"]["name"], "Stub Seed")
        self.assertEqual(payload["candidates"][0]["candidate"], "Stub Candidate")
        self.assertEqual(payload["candidates"][0]["state"], "CORROBORATED")
        self.assertTrue(payload["candidates"][0]["receipts_verified"])
        self.assertEqual(
            [s["step"] for s in payload["trace"]][0], "resolve_seed"
        )

    def test_unresolvable_seed_refuses_with_a_trace(self):
        with mock.patch("second_opinion.server.LiveAdapter", _StubLiveAdapter):
            status, _, payload = self.json_get(
                "/api/audit?seed=Nowhere%20Band&mode=live"
            )
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["refusal"]["code"], "seed resolution failed")
        self.assertEqual(payload["trace"][0]["status"], "failed")

    def test_timeout_is_named_precisely(self):
        class SlowAdapter(_StubLiveAdapter):
            def resolve_seed(self, query):
                raise UpstreamTimeout("request to Qloo timed out")

        with mock.patch("second_opinion.server.LiveAdapter", SlowAdapter):
            status, _, payload = self.json_get("/api/audit?seed=Laufey&mode=live")
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["refusal"]["code"], "upstream timeout")

    def test_rejected_credential_is_named_precisely(self):
        class RejectingAdapter(_StubLiveAdapter):
            def resolve_seed(self, query):
                raise AuthenticationError("Qloo rejected the credential (HTTP 401)")

        with mock.patch("second_opinion.server.LiveAdapter", RejectingAdapter):
            status, _, payload = self.json_get("/api/audit?seed=Laufey&mode=live")
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["refusal"]["code"], "authentication failed")

    def test_missing_credential_inside_adapter_is_503(self):
        class NoKeyAdapter:
            def __init__(self, *a, **k):
                raise MissingCredentials("QLOO_API_KEY is not set")

        with mock.patch("second_opinion.server.LiveAdapter", NoKeyAdapter):
            status, _, payload = self.json_get("/api/audit?seed=Laufey&mode=live")
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])
        self.assertIn("QLOO_API_KEY", payload["error"])

    def test_insufficient_seed_metadata_refuses(self):
        class BareSeedAdapter(_StubLiveAdapter):
            def seed_payload(self, seed_id):
                return {"entity_id": seed_id, "name": "Bare", "properties": {}, "tags": []}

        with mock.patch("second_opinion.server.LiveAdapter", BareSeedAdapter):
            status, _, payload = self.json_get("/api/audit?seed=Laufey&mode=live")
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["refusal"]["code"], "insufficient seed metadata")
        self.assertEqual(payload["refusal"]["facts"]["seed_tag_count"], 0)

    def test_seed_record_fetch_failure_is_a_refusal_not_a_crash(self):
        class LateFailAdapter(_StubLiveAdapter):
            def seed_payload(self, seed_id):
                raise UpstreamTimeout("request to Qloo timed out")

        with mock.patch("second_opinion.server.LiveAdapter", LateFailAdapter):
            status, _, payload = self.json_get("/api/audit?seed=Laufey&mode=live")
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["refusal"]["code"], "upstream timeout")

    def test_empty_candidate_set_still_returns_a_report(self):
        class EmptyAdapter(_StubLiveAdapter):
            def candidates(self, seed_id):
                return []

        with mock.patch("second_opinion.server.LiveAdapter", EmptyAdapter):
            status, _, payload = self.json_get("/api/audit?seed=Laufey&mode=live")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["candidates"], [])


if __name__ == "__main__":
    unittest.main()
