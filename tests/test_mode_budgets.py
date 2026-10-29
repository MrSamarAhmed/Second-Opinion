"""Fixture mode versus live mode: what each one is allowed to cost.

The audit endpoint's budget exists to protect a Qloo quota. Fixture mode reads
`data/fixtures/**` from local disk and contacts nothing, so charging a fixture
click to the live bucket both blocked local work and told the reader a falsehood.
These tests run the real `FixtureAdapter` against a small hand-written fixture
directory created in a temp folder — no captured Qloo response is involved, which
is why this file does not skip on a clean checkout — and assert:

* a fixture audit performs zero network I/O;
* each mode has its own bucket, and exhausting one leaves the other usable;
* a 429 names the budget that ran out, its mode, and an honest retry time;
* the live quota guard still refuses the third audit in a burst of one.

`tests/test_rate_limit.py` covers the same limits with the live adapter stubbed;
this file covers them with the fixture adapter actually working.
"""

import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from second_opinion.adapters import FixtureAdapter
from second_opinion.throttle import (
    AuditThrottle,
    set_default_throttle,
    throttle_for_mode,
)

from .test_rate_limit import CountingAdapter, ThrottleTestCase, _request

# Hand-written metadata, invented for this test. The first candidate shares
# genres, themes, audience and style tags with the seed, so the audit lands on a
# real CORROBORATED report rather than a refusal.
_SEED = {
    "entity_id": "synthetic-seed-1",
    "name": "Test Seed Artist",
    "properties": {
        "primary_genres": ["art punk"],
        "secondary_genres": ["post-punk"],
        "lyrical_themes": ["alienation", "city life"],
    },
    "tags": [
        {"id": "a1", "name": "late-night listeners", "type": "urn:tag:audience:qloo"},
        {"id": "s1", "name": "raw production", "type": "urn:tag:style:qloo"},
    ],
}

_CANDIDATES = [
    {
        "entity_id": "synthetic-cand-1",
        "name": "Test Match One",
        "query": {"affinity": 0.91},
        "properties": {
            "primary_genres": ["art punk", "noise rock"],
            "secondary_genres": [],
            "lyrical_themes": ["alienation", "protest"],
        },
        "tags": [
            {"id": "a2", "name": "late-night listeners", "type": "urn:tag:audience:qloo"},
            {"id": "s2", "name": "raw production", "type": "urn:tag:style:qloo"},
        ],
    },
    {
        "entity_id": "synthetic-cand-2",
        "name": "Test Match Two",
        "query": {"affinity": 0.44},
        "properties": {
            "primary_genres": ["krautrock"],
            "secondary_genres": [],
            "lyrical_themes": ["motorik"],
        },
        "tags": [{"id": "a3", "name": "train listeners", "type": "urn:tag:audience:qloo"}],
    },
]


def write_fixtures(root: Path) -> Path:
    """Lay out the two directories `FixtureAdapter` expects, with invented data."""
    (root / "search").mkdir(parents=True, exist_ok=True)
    (root / "pair").mkdir(parents=True, exist_ok=True)
    (root / "search" / "search_test_seed_artist.json").write_text(json.dumps({"results": [_SEED]}))
    (root / "pair" / "pair_test_seed_artist.json").write_text(
        json.dumps({"results": {"entities": _CANDIDATES}})
    )
    return root


def _tracked(mode: str) -> int:
    """How many distinct client buckets that mode's limiter is holding."""
    return throttle_for_mode(mode).limiter.tracked_keys()


class _NoLiveAdapter:
    """Standing where the Qloo client would be built.

    Refusing at construction is the structural proof: a fixture audit that never
    builds a client cannot send a request.
    """

    def __init__(self, *args, **kwargs):
        raise AssertionError("fixture mode constructed a live Qloo client")


_REAL_URLOPEN = urllib.request.urlopen


def _only_local_urlopen(request, *args, **kwargs):
    """Let the test client reach 127.0.0.1; fail on anything else.

    `adapters.py` and this test share one `urllib.request`, so the guard has to
    allow the loopback call the test itself makes and refuse every other target.
    Any non-local URL during a fixture audit is an outbound request.
    """
    url = getattr(request, "full_url", str(request))
    if "127.0.0.1" in url or "localhost" in url:
        return _REAL_URLOPEN(request, *args, **kwargs)
    raise AssertionError(f"fixture mode opened a non-local URL: {url}")


class FixtureThrottleTestCase(ThrottleTestCase):
    """The same harness, plus a working fixture directory on disk."""

    fixture_per_minute = 60
    fixture_burst = 2
    fixture_concurrent = 4

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = write_fixtures(Path(tmp.name))
        # Only the fixture root is redirected; the production adapter class and
        # its loader run for real on the synthetic files.
        patcher = mock.patch("second_opinion.server.FixtureAdapter", lambda *a, **k: FixtureAdapter(root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def fixture_audit(self, seed="Test Seed Artist"):
        query = urllib.parse.urlencode({"seed": seed, "mode": "fixture"})
        status, headers, body = _request(self.port, f"/api/audit?{query}", self.forward_for)
        return status, headers, json.loads(body)


class TestFixtureModeIsLocalOnly(FixtureThrottleTestCase):
    def test_a_fixture_audit_returns_a_real_report(self):
        status, _, payload = self.fixture_audit()
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["seed"]["name"], "Test Seed Artist")
        self.assertIn("CORROBORATED", {c["state"] for c in payload["candidates"]})

    def test_fixture_audit_opens_no_socket_and_builds_no_qloo_client(self):
        with mock.patch("second_opinion.adapters.urllib.request.urlopen", _only_local_urlopen):
            with mock.patch("second_opinion.server.LiveAdapter", _NoLiveAdapter):
                status, _, payload = self.fixture_audit()
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])

    def test_repeated_fixture_audits_still_never_reach_the_network(self):
        with mock.patch("second_opinion.adapters.urllib.request.urlopen", _only_local_urlopen):
            with mock.patch("second_opinion.server.LiveAdapter", _NoLiveAdapter):
                for _ in range(self.fixture_burst):
                    self.assertEqual(self.fixture_audit()[0], 200)
                self.assertEqual(self.fixture_audit()[0], 429)


class TestSeparateBudgets(FixtureThrottleTestCase):
    burst = 1
    fixture_burst = 2

    def test_many_fixture_audits_leave_the_live_budget_untouched(self):
        for _ in range(self.fixture_burst):
            self.assertEqual(self.fixture_audit()[0], 200)
        self.assertEqual(self.fixture_audit()[0], 429)
        # One live audit was always available, and still is.
        status, _, payload = self.audit(seed="Fontaines D.C.")
        self.assertEqual(status, 200, payload)
        self.assertEqual([call[1] for call in CountingAdapter.calls], ["Fontaines D.C."])

    def test_an_exhausted_live_budget_leaves_fixture_work_running(self):
        self.assertEqual(self.audit()[0], 200)
        self.assertEqual(self.audit()[0], 429)
        for _ in range(self.fixture_burst):
            self.assertEqual(self.fixture_audit()[0], 200)

    def test_the_live_quota_guard_is_still_enforced(self):
        # The protection must not have been weakened on the way past this fix:
        # burst of one live audit, then refusal, with no extra upstream call.
        self.assertEqual(self.audit()[0], 200)
        status, headers, payload = self.audit()
        self.assertEqual(status, 429)
        self.assertEqual(payload["code"], "rate_limited")
        self.assertTrue(payload["limit"]["spends_quota"])
        self.assertEqual(len(CountingAdapter.calls), 1)
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)

    def test_each_mode_keeps_its_own_bucket_table(self):
        before = _tracked("live")
        for _ in range(self.fixture_burst + 2):
            self.fixture_audit()
        self.assertEqual(_tracked("live"), before, "fixture traffic must not create live buckets")
        self.assertEqual(_tracked("fixture"), 1)


class TestRefusalIsHonest(FixtureThrottleTestCase):
    fixture_burst = 1

    def test_a_fixture_refusal_names_the_fixture_budget(self):
        self.fixture_audit()
        status, headers, payload = self.fixture_audit()
        self.assertEqual(status, 429)
        self.assertEqual(payload["mode"], "fixture")
        self.assertEqual(payload["limit"]["mode"], "fixture")
        self.assertFalse(payload["limit"]["spends_quota"])
        self.assertIn("fixture", payload["error"])
        # It must not claim a live budget was spent, or that quota is involved.
        self.assertNotIn("live", payload["error"])
        self.assertNotIn("quota", payload["error"])
        self.assertEqual(str(payload["retry_after_seconds"]), headers["Retry-After"])

    def test_a_live_refusal_names_the_live_budget(self):
        self.burst = 1
        self.install_budgets()
        self.audit()
        status, _, payload = self.audit()
        self.assertEqual(status, 429)
        self.assertEqual(payload["mode"], "live")
        self.assertTrue(payload["limit"]["spends_quota"])
        self.assertIn("live", payload["error"])
        self.assertNotIn("fixture", payload["error"])

    def test_retry_after_is_a_useful_positive_number(self):
        # 60/minute is one token a second, so the wait must be reported in
        # seconds a reader can act on: never zero, never absurd.
        set_default_throttle(
            AuditThrottle(per_minute=60, burst=1, concurrent=4, mode="fixture"), "fixture"
        )
        self.fixture_audit()
        _, headers, payload = self.fixture_audit()
        wait = payload["retry_after_seconds"]
        self.assertGreaterEqual(wait, 1)
        self.assertLessEqual(wait, 2)
        self.assertEqual(headers["Retry-After"], str(wait))

    def test_a_busy_fixture_response_names_its_mode(self):
        # Deterministic occupancy: the fixture agent is held open by the first
        # request, so the second one meets the in-flight gate, not the bucket.
        started = threading.Event()
        release = threading.Event()

        class _HeldAgent:
            def __init__(self, *args, **kwargs):
                pass

            def audit(self, seed):
                started.set()
                release.wait(timeout=10)
                return {"ok": True, "seed": {"name": seed}, "candidates": []}

        set_default_throttle(
            AuditThrottle(per_minute=60, burst=10, concurrent=1, mode="fixture"), "fixture"
        )
        results = {}

        def first():
            results["first"] = self.fixture_audit()

        patcher = mock.patch("second_opinion.server.SecondOpinionAgent", _HeldAgent)
        patcher.start()
        self.addCleanup(patcher.stop)
        thread = threading.Thread(target=first)
        thread.start()
        self.addCleanup(lambda: (release.set(), thread.join(timeout=10)))
        self.assertTrue(started.wait(timeout=5), "the first fixture audit should be running")

        status, headers, payload = self.fixture_audit()
        release.set()
        thread.join(timeout=15)
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "audit_busy")
        self.assertEqual(payload["mode"], "fixture")
        self.assertFalse(payload["limit"]["spends_quota"])
        self.assertEqual(headers["Retry-After"], "2")
        self.assertEqual(results["first"][0], 200)


def function_body(source: str, name: str) -> str:
    """The brace-balanced body of a top-level `function name(...)` in the page."""
    start = source.index(f"function {name}(")
    depth, index = 0, source.index("{", start)
    for cursor in range(index, len(source)):
        char = source[cursor]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[index : cursor + 1]
    raise AssertionError(f"unbalanced body for {name}")


class TestFrontendDoesNotRetry(unittest.TestCase):
    """The page must inform a refused visitor, not hammer the endpoint.

    These read the shipped source rather than a browser: what is being pinned is
    the shape of the code — one timer, and no request inside it. The rendered
    result is verified separately by driving the page.
    """

    source = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()

    def _function_body(self, name: str) -> str:
        return function_body(self.source, name)

    def test_the_only_timer_in_the_page_is_the_countdown(self):
        body = self._function_body("countdown")
        self.assertGreaterEqual(self.source.count("setTimeout("), 1)
        # Every timer the page owns lives in that one function.
        self.assertEqual(self.source.count("setTimeout("), body.count("setTimeout("))
        self.assertNotIn("setInterval(", self.source)

    def test_the_countdown_never_sends_a_request(self):
        body = self._function_body("countdown")
        for call in ("fetch(", "selectSeed(", "XMLHttpRequest", "location.reload"):
            self.assertNotIn(call, body, f"the countdown must not trigger {call}")
        # Only two requests exist in the page at all: the seed list and one audit.
        self.assertEqual(self.source.count("fetch("), 2)

    def test_a_new_audit_clears_the_previous_countdown(self):
        self.assertIn("stopCountdown();", self._function_body("selectSeed"))
        self.assertIn("stopCountdown();", self._function_body("render"))

    def test_the_refusal_hint_distinguishes_the_two_budgets(self):
        self.assertIn("One live audit costs three Qloo requests", self.source)
        self.assertIn("sends nothing to Qloo", self.source)
        # The quota sentence is chosen by the flag the server sends, not assumed.
        self.assertIn("data.limit.spends_quota", self.source)


class TestFrontendIgnoresSupersededResponses(unittest.TestCase):
    """A slow audit must not overwrite the seed the visitor clicked last.

    The server allows more than one audit in flight and a live audit takes as long
    as Qloo takes, so two quick clicks can come back in either order. Driving the
    page showed the earlier response rendering after the later one — the panel
    naming one seed while the sidebar highlighted another. Each click now takes a
    ticket and a response is only rendered while its ticket is still the newest;
    these tests pin that shape in the shipped source.
    """

    source = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()

    def test_each_click_takes_a_new_ticket(self):
        body = function_body(self.source, "selectSeed")
        self.assertIn("let REQUEST_SEQ = 0;", self.source)
        self.assertIn("++REQUEST_SEQ", body)

    def test_a_report_is_rendered_only_for_the_newest_click(self):
        body = function_body(self.source, "selectSeed")
        self.assertLess(body.index("ticket !== REQUEST_SEQ"),
                        body.index("render(data, seed, res.status)"))
        # The one call site that paints the panel is the guarded one.
        self.assertEqual(self.source.count("render(data, seed, res.status)"), 1)

    def test_a_late_network_error_cannot_erase_the_current_report(self):
        body = function_body(self.source, "selectSeed")
        self.assertEqual(body.count("ticket !== REQUEST_SEQ"), 2,
                         "both the response path and the failure path must be guarded")
        self.assertLess(body.rindex("ticket !== REQUEST_SEQ"),
                        body.index("Audit request failed"))


if __name__ == "__main__":
    unittest.main()
