"""HTTP tests for the audit throttle: what a visitor may cost the operator.

The adapter is stubbed, so every call the stub receives is a call that would
have been a Qloo request. The point of most of these tests is therefore a
*count*: a refused request must leave the count unchanged.

Client bucketing is driven through `X-Forwarded-For` because the server reads
only the rightmost entry — see `second_opinion.throttle`.
"""

import io
import json
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from second_opinion.adapters import AdapterError
from second_opinion.server import Handler
from second_opinion.throttle import AuditThrottle, MAX_SEED_LENGTH, set_default_throttle

from .test_server import SECRET, _StubLiveAdapter


class CountingAdapter(_StubLiveAdapter):
    """The stub from test_server.py, counting the upstream calls it would make."""

    calls = []
    lock = threading.Lock()
    block_on = None  # set to a threading.Event to hold an audit open
    raise_runtime = False

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.calls = []
            cls.block_on = None
            cls.raise_runtime = False

    def resolve_seed(self, query):
        with CountingAdapter.lock:
            CountingAdapter.calls.append(("resolve_seed", query))
        if CountingAdapter.raise_runtime:
            # A bug inside the audit path: a message the operator needs in the
            # log, and that a client must never receive.
            raise RuntimeError("boom: candidate payload unreadable")
        if CountingAdapter.block_on is not None:
            CountingAdapter.block_on.wait(timeout=10)
        return super().resolve_seed(query)


class _QuietHandler(Handler):
    def log_message(self, fmt, *args):
        pass


def _request(port, path, forward_for=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if forward_for is not None:
        request.add_header("X-Forwarded-For", forward_for)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class ThrottleTestCase(unittest.TestCase):
    """A server per test class and a deliberately small budget per test.

    Both budgets are installed explicitly: a test that only pins the live budget
    would otherwise inherit whatever the ambient environment says about fixture
    audits, and the two are meant to be independent.
    """

    per_minute = 6
    burst = 2
    concurrent = 4
    fixture_per_minute = 60
    fixture_burst = 20
    fixture_concurrent = 4
    forward_for = "203.0.113.9"

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

    def install_budgets(self):
        set_default_throttle(
            AuditThrottle(
                per_minute=self.per_minute,
                burst=self.burst,
                concurrent=self.concurrent,
                mode="live",
            ),
            "live",
        )
        set_default_throttle(
            AuditThrottle(
                per_minute=self.fixture_per_minute,
                burst=self.fixture_burst,
                concurrent=self.fixture_concurrent,
                mode="fixture",
            ),
            "fixture",
        )

    def setUp(self):
        CountingAdapter.reset()
        self.install_budgets()
        self.addCleanup(set_default_throttle, None)
        patcher = mock.patch("second_opinion.server.LiveAdapter", CountingAdapter)
        patcher.start()
        self.addCleanup(patcher.stop)

    def audit(self, seed="Laufey", forward_for=None, **params):
        query = urllib.parse.urlencode({"seed": seed, "mode": "live", **params})
        status, headers, body = _request(
            self.port, f"/api/audit?{query}", forward_for or self.forward_for
        )
        return status, headers, json.loads(body)

    def get(self, path, forward_for=None):
        status, headers, body = _request(self.port, path, forward_for)
        return status, headers, json.loads(body)


class TestAuditBudget(ThrottleTestCase):
    burst = 2

    def test_normal_audits_still_work_inside_the_budget(self):
        # The judge workflow must stay usable: a real report, from the stub,
        # with real candidates and receipts.
        for _ in range(2):
            status, _, payload = self.audit()
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["candidates"][0]["state"], "CORROBORATED")
        self.assertEqual(len(CountingAdapter.calls), 2)

    def test_the_next_request_is_refused_before_any_upstream_call(self):
        for _ in range(2):
            self.assertEqual(self.audit()[0], 200)
        status, headers, payload = self.audit()
        self.assertEqual(status, 429)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "rate_limited")
        self.assertEqual(len(CountingAdapter.calls), 2, "a refused audit must cost nothing")
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        self.assertEqual(payload["retry_after_seconds"], int(headers["Retry-After"]))

    def test_refused_response_advertises_the_active_limits(self):
        for _ in range(self.burst):
            self.audit()
        _, _, payload = self.audit()
        self.assertEqual(
            payload["limit"],
            {
                "mode": "live",
                "per_minute": self.per_minute,
                "burst": self.burst,
                "concurrent": self.concurrent,
                "spends_quota": True,
                "storage": "in-memory",
                "resets_on_restart": True,
            },
        )
        self.assertEqual(payload["mode"], "live")

    def test_refill_restores_service_without_a_restart(self):
        throttle = AuditThrottle(per_minute=60, burst=1, concurrent=4, mode="live")
        set_default_throttle(throttle, "live")
        self.assertEqual(self.audit()[0], 200)
        self.assertEqual(self.audit()[0], 429)
        # 60/minute is one token a second; the bucket refills in process time.
        time.sleep(1.1)
        self.assertEqual(self.audit()[0], 200)

    def test_a_restart_clears_the_buckets(self):
        # Documents the known limitation: state is memory-only, so redeploying
        # or an idle spin-down hands every client a fresh burst.
        for _ in range(self.burst):
            self.audit()
        self.assertEqual(self.audit()[0], 429)
        self.install_budgets()
        self.assertEqual(self.audit()[0], 200)


class TestClientKeying(ThrottleTestCase):
    burst = 1

    def test_spoofed_prefix_does_not_buy_a_second_bucket(self):
        self.assertEqual(self.audit(forward_for="1.1.1.1, 203.0.113.9")[0], 200)
        status, _, _ = self.audit(forward_for="9.9.9.9, 203.0.113.9")
        self.assertEqual(status, 429)
        self.assertEqual(len(CountingAdapter.calls), 1)

    def test_distinct_real_clients_have_distinct_budgets(self):
        self.assertEqual(self.audit(forward_for="203.0.113.9")[0], 200)
        self.assertEqual(self.audit(forward_for="198.51.100.1")[0], 200)
        self.assertEqual(len(CountingAdapter.calls), 2)

    def test_no_forwarded_header_shares_one_bucket(self):
        # Behind no proxy every visitor is the same peer address; that is one
        # global allowance, which fails closed rather than open.
        self.assertEqual(self.audit(forward_for="")[0], 200)
        self.assertEqual(self.audit(forward_for="")[0], 429)

    def test_free_endpoints_are_never_throttled(self):
        # Only the endpoint that spends quota is limited; the page and the
        # hand-authored seed list stay free for a judge reloading them.
        self.audit()
        for _ in range(10):
            self.assertEqual(self.get("/api/seeds")[0], 200)
            self.assertEqual(self.get("/api/health")[0], 200)
            self.assertEqual(self.get("/api/rules")[0], 200)
        self.assertEqual(len(CountingAdapter.calls), 1)


class TestValidationBeforeSpend(ThrottleTestCase):
    burst = 1

    def test_overlong_seed_is_rejected_without_cost_or_spend(self):
        status, _, payload = self.audit(seed="A" * (MAX_SEED_LENGTH + 1))
        self.assertEqual(status, 400)
        self.assertIn("too long", payload["error"])
        self.assertEqual(CountingAdapter.calls, [])
        # And it did not consume the budget either.
        self.assertEqual(self.audit()[0], 200)

    def test_blank_seed_and_bad_mode_cost_nothing(self):
        self.assertEqual(self.audit(seed="   ")[0], 400)
        self.assertEqual(self.audit(seed="Laufey", mode="sneaky")[0], 400)
        self.assertEqual(self.audit(seed="Laufey", threshold="abc")[0], 400)
        self.assertEqual(CountingAdapter.calls, [])
        self.assertEqual(self.audit()[0], 200)

    def test_unknown_mode_does_not_echo_the_client_value(self):
        marker = "sneaky-mode-with-client-text"
        _, _, payload = self.audit(seed="Laufey", mode=marker)
        self.assertNotIn(marker, json.dumps(payload))

    def test_fixture_mode_does_not_empty_the_live_budget(self):
        # The reported defect: a fixture audit used to draw on the quota guard,
        # so a page in fixture mode could be refused with "audit rate limit
        # reached" while having spent nothing. Fixture requests now hit their own
        # bucket, and the live one is untouched.
        self.burst = 1
        self.install_budgets()
        query = urllib.parse.urlencode({"seed": "Laufey", "mode": "fixture"})
        for _ in range(3):
            status = _request(self.port, f"/api/audit?{query}", self.forward_for)[0]
            self.assertIn(status, (200, 503))  # 503 when this checkout has no fixtures
        self.assertEqual(CountingAdapter.calls, [])
        self.assertEqual(self.audit()[0], 200)
        self.assertEqual(self.audit()[0], 429)

    def test_a_live_refusal_does_not_block_fixture_requests(self):
        # The other direction: exhausting the live budget must not stop someone
        # from browsing the local captures. Same client, same seed, other mode.
        self.burst = 1
        self.install_budgets()
        self.assertEqual(self.audit()[0], 200)
        self.assertEqual(self.audit()[0], 429)
        query = urllib.parse.urlencode({"seed": "Laufey", "mode": "fixture"})
        status, _, raw = _request(self.port, f"/api/audit?{query}", self.forward_for)
        payload = json.loads(raw)
        self.assertNotEqual(status, 429)
        self.assertNotEqual(payload.get("code"), "rate_limited")


class TestConcurrencyGate(ThrottleTestCase):
    concurrent = 1

    def test_a_second_audit_is_refused_while_one_is_in_flight(self):
        gate = threading.Event()
        CountingAdapter.block_on = gate
        done = {}

        def first():
            done["response"] = self.audit(seed="Fontaines D.C.")

        thread = threading.Thread(target=first)
        thread.start()
        self.addCleanup(lambda: (gate.set(), thread.join(timeout=10)))

        for _ in range(100):
            if CountingAdapter.calls:
                break
            time.sleep(0.02)
        self.assertTrue(CountingAdapter.calls, "the first audit should have started")

        status, headers, payload = self.audit(seed="Little Simz")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "audit_busy")
        self.assertIn("running", payload["error"])
        self.assertEqual(headers["Retry-After"], "2")
        self.assertEqual(
            [call[1] for call in CountingAdapter.calls],
            ["Fontaines D.C."],
            "the busy request must not reach Qloo too",
        )

        gate.set()
        thread.join(timeout=15)
        self.assertEqual(done["response"][0], 200)


class TestNothingLeaks(ThrottleTestCase):
    burst = 1
    per_minute = 6

    def setUp(self):
        super().setUp()
        env = {"QLOO_API_KEY": SECRET}
        patcher = mock.patch.dict("os.environ", env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rate_limit_response_carries_no_credential(self):
        self.audit()
        status, _, body = _request(
            self.port, "/api/audit?seed=Laufey&mode=live", self.forward_for
        )
        self.assertEqual(status, 429)
        self.assertNotIn(SECRET, body.decode("utf-8", "replace"))

    def test_refusal_detail_containing_the_key_is_scrubbed(self):
        # The agent turns an adapter failure into a refusal at HTTP 200, and the
        # refusal detail is the exception text. If a message ever carried the
        # credential, the redaction net has to catch it here rather than at 4xx.
        class LeakyAdapter(CountingAdapter):
            def resolve_seed(self, query):
                raise AdapterError(f"Qloo refused: X-Api-Key {SECRET}")

        with mock.patch("second_opinion.server.LiveAdapter", LeakyAdapter):
            status, _, body = _request(
                self.port, "/api/audit?seed=Laufey&mode=live", self.forward_for
            )
        text = body.decode("utf-8", "replace")
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET, text)
        self.assertIn("[credential redacted]", text)
        self.assertFalse(json.loads(text)["ok"])

    def test_unexpected_bug_becomes_a_bounded_500(self):
        CountingAdapter.raise_runtime = True
        logs = io.StringIO()
        with mock.patch("sys.stderr", logs):
            status, _, body = _request(
                self.port, "/api/audit?seed=Laufey&mode=live", self.forward_for
            )
        text = body.decode("utf-8", "replace")
        self.assertEqual(status, 500)
        self.assertIn("internal audit error", text)
        # Nothing about the failure reaches the client...
        self.assertNotIn(SECRET, text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("boom", text)
        # ...but the operator still sees it, and the log stays credential-free.
        self.assertIn("RuntimeError", logs.getvalue())
        self.assertIn("boom: candidate payload unreadable", logs.getvalue())
        self.assertNotIn(SECRET, logs.getvalue())

    def test_seeds_endpoint_reports_readiness_not_the_key(self):
        _, _, payload = self.get("/api/seeds")
        self.assertTrue(payload["environment"]["live_ready"])
        self.assertEqual(set(payload["environment"]), {"live_ready", "host"})


class TestLogHygiene(unittest.TestCase):
    """The access line is the one place the server writes client text to stderr.

    It can never contain the credential because the credential is a header, not
    a URL parameter — `tests/test_adapters.py::test_request_carries_the_key_in_a_header_not_the_url`
    pins that. These tests cover the other half: the line is bounded, so a rude
    request target cannot bloat the log.
    """

    class _Target:
        def address_string(self):
            return "10.0.0.1"

        log_message = Handler.log_message

    def _line(self, fmt, *args):
        logs = io.StringIO()
        with mock.patch("sys.stderr", logs):
            self._Target().log_message(fmt, *args)
        return logs.getvalue()

    def test_normal_request_line_is_unchanged(self):
        line = self._line('"%s" %s %s', "GET /api/seeds", 200, "-")
        self.assertEqual(line.strip(), '10.0.0.1 - "GET /api/seeds" 200 -')

    def test_an_absurd_request_target_is_truncated(self):
        line = self._line('"%s" %s %s', "GET /api/audit?seed=" + "A" * 5000, 400, "-")
        self.assertLess(len(line), 400)
        self.assertIn("truncated", line)

    def test_a_real_artist_name_is_not_truncated(self):
        line = self._line('"%s" %s %s', "GET /api/audit?seed=Fontaines%20D.C.", 200, "-")
        self.assertNotIn("truncated", line)
        self.assertIn("Fontaines", line)


if __name__ == "__main__":
    unittest.main()
