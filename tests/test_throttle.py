"""Unit tests for the audit throttle: bucket math, keying, pruning and the gate.

No server and no network: these assert the arithmetic the HTTP layer relies on,
using an injected clock so refill is tested deterministically instead of by
sleeping.
"""

import threading
import unittest
from unittest import mock

from second_opinion.throttle import (
    AuditThrottle,
    ConcurrencyGate,
    MAX_SEED_LENGTH,
    RateLimiter,
    client_key,
    rightmost_forwarded_client,
    settings_from_env,
)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestRateLimiter(unittest.TestCase):
    def test_burst_is_allowed_then_refused(self):
        clock = FakeClock()
        limiter = RateLimiter(capacity=3, refill_per_minute=6, clock=clock)
        outcomes = [limiter.allow("k")[0] for _ in range(4)]
        self.assertEqual(outcomes, [True, True, True, False])

    def test_refusal_costs_no_token(self):
        # A refused request must not drain the bucket further, or hammering would
        # lengthen the lockout beyond what the refill actually owes.
        clock = FakeClock()
        limiter = RateLimiter(capacity=1, refill_per_minute=60, clock=clock)
        self.assertEqual(limiter.allow("k"), (True, 0.0))
        clock.advance(1.0)  # exactly one token back
        self.assertEqual(limiter.allow("k")[0], True)

    def test_refill_restores_allowance(self):
        clock = FakeClock()
        limiter = RateLimiter(capacity=2, refill_per_minute=6, clock=clock)
        limiter.allow("k")
        limiter.allow("k")
        self.assertEqual(limiter.allow("k")[0], False)
        clock.advance(20)  # 6/minute = one token every 10 seconds
        self.assertEqual(limiter.allow("k")[0], True)

    def test_retry_after_is_the_wait_for_one_token(self):
        clock = FakeClock()
        limiter = RateLimiter(capacity=1, refill_per_minute=6, clock=clock)
        limiter.allow("k")
        allowed, wait = limiter.allow("k")
        self.assertFalse(allowed)
        self.assertAlmostEqual(wait, 10.0, places=2)

    def test_retry_after_is_capped(self):
        clock = FakeClock()
        # 1 per hour: the honest wait is long, but a client must not be told to
        # come back in hours through a header meant for a demo budget.
        limiter = RateLimiter(capacity=1, refill_per_minute=1, clock=clock)
        limiter.allow("k")
        _, wait = limiter.allow("k")
        self.assertLessEqual(wait, 3600.0)
        self.assertGreaterEqual(wait, 60.0)

    def test_capacity_is_a_ceiling_so_idle_time_cannot_bank_requests(self):
        clock = FakeClock()
        limiter = RateLimiter(capacity=3, refill_per_minute=60, clock=clock)
        clock.advance(600)
        outcomes = [limiter.allow("k")[0] for _ in range(4)]
        self.assertEqual(outcomes, [True, True, True, False])

    def test_keys_are_independent(self):
        clock = FakeClock()
        limiter = RateLimiter(capacity=1, refill_per_minute=6, clock=clock)
        self.assertEqual(limiter.allow("a")[0], True)
        self.assertEqual(limiter.allow("b")[0], True)
        self.assertEqual(limiter.allow("a")[0], False)

    def test_bucket_table_stays_bounded(self):
        clock = FakeClock()
        limiter = RateLimiter(capacity=1, refill_per_minute=6, max_keys=20, clock=clock)
        for index in range(500):
            clock.advance(0.001)
            limiter.allow(f"client-{index}")
        self.assertLessEqual(limiter.tracked_keys(), 20)

    def test_pruning_forgets_the_idlest_first(self):
        clock = FakeClock()
        limiter = RateLimiter(capacity=5, refill_per_minute=600, max_keys=2, clock=clock)
        limiter.allow("old")
        clock.advance(1)
        limiter.allow("mid")
        clock.advance(1)
        limiter.allow("new")  # forces a prune of "old"
        self.assertNotIn("old", limiter._buckets)
        self.assertIn("new", limiter._buckets)

    def test_invalid_construction_is_refused(self):
        for kwargs in ({"capacity": 0}, {"refill_per_minute": 0}, {"max_keys": 0}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    RateLimiter(**kwargs)

    def test_parallel_calls_respect_the_burst_exactly(self):
        clock = FakeClock()
        limiter = RateLimiter(capacity=5, refill_per_minute=1, clock=clock)
        results = []
        lock = threading.Lock()

        def hammer():
            allowed = limiter.allow("shared")[0]
            with lock:
                results.append(allowed)

        threads = [threading.Thread(target=hammer) for _ in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sum(1 for r in results if r), 5)
        self.assertEqual(len(results), 40)


class TestConcurrencyGate(unittest.TestCase):
    def test_gate_opens_only_up_to_the_limit(self):
        gate = ConcurrencyGate(2)
        self.assertTrue(gate.try_acquire())
        self.assertTrue(gate.try_acquire())
        self.assertFalse(gate.try_acquire())
        gate.release()
        self.assertTrue(gate.try_acquire())

    def test_gate_never_over_admits_under_contention(self):
        gate = ConcurrencyGate(3)
        peak = []
        current = [0]
        lock = threading.Lock()

        def worker():
            if not gate.try_acquire():
                return
            with lock:
                current[0] += 1
                peak.append(current[0])
            with lock:
                current[0] -= 1
            gate.release()

        threads = [threading.Thread(target=worker) for _ in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertLessEqual(max(peak), 3)

    def test_gate_limit_must_be_positive(self):
        with self.assertRaises(ValueError):
            ConcurrencyGate(0)


class TestClientKey(unittest.TestCase):
    def test_key_is_opaque(self):
        key = client_key("203.0.113.9")
        self.assertNotIn("203.0.113.9", key)
        self.assertRegex(key, r"^[0-9a-f]{16}$")

    def test_key_is_stable_and_distinct(self):
        self.assertEqual(client_key("203.0.113.9"), client_key("203.0.113.9"))
        self.assertNotEqual(client_key("203.0.113.9"), client_key("203.0.113.10"))

    def test_missing_or_blank_input_collapses_to_one_bucket(self):
        # Failing toward a shared bucket is the safe direction: an operator who
        # forgets to read the forwarded header gets one global limit, not none.
        self.assertEqual(client_key(None), client_key(""))
        self.assertEqual(client_key("   "), client_key(None))

    def test_last_entry_of_a_header_list_is_the_one_that_counts(self):
        self.assertEqual(client_key("1.1.1.1, 203.0.113.9"), client_key("203.0.113.9"))


class TestRightmostForwardedClient(unittest.TestCase):
    def test_reads_the_proxy_appended_entry(self):
        self.assertEqual(
            rightmost_forwarded_client("198.51.100.7, 203.0.113.9"), "203.0.113.9"
        )

    def test_ignores_the_client_supplied_prefix(self):
        # The left-hand value is whatever the visitor typed; two different
        # spoofs must not produce two different clients here.
        self.assertEqual(
            rightmost_forwarded_client("1.1.1.1, 203.0.113.9"),
            rightmost_forwarded_client("9.9.9.9, 203.0.113.9"),
        )

    def test_single_entry_and_absent_header(self):
        self.assertEqual(rightmost_forwarded_client("203.0.113.9"), "203.0.113.9")
        self.assertEqual(rightmost_forwarded_client(None), "")
        self.assertEqual(rightmost_forwarded_client("  ,  "), "")


class TestSettings(unittest.TestCase):
    def test_defaults_when_the_environment_is_silent(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                settings_from_env(),
                {"per_minute": 6, "burst": 3, "concurrent": 2, "max_keys": 2048},
            )

    def test_overrides_are_read(self):
        env = {
            "SO_AUDIT_PER_MINUTE": "30",
            "SO_AUDIT_BURST": "10",
            "SO_MAX_CONCURRENT_AUDITS": "4",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            settings = settings_from_env()
        self.assertEqual(settings["per_minute"], 30)
        self.assertEqual(settings["burst"], 10)
        self.assertEqual(settings["concurrent"], 4)

    def test_garbage_and_evil_values_cannot_disable_the_limit(self):
        # 0 or negative must not mean "unlimited", and a huge number is clamped.
        for raw in ("0", "-5", "abc", "", "  "):
            with self.subTest(raw=raw), mock.patch.dict(
                "os.environ", {"SO_AUDIT_PER_MINUTE": raw}, clear=True
            ):
                self.assertEqual(settings_from_env()["per_minute"], 6)
        with mock.patch.dict("os.environ", {"SO_AUDIT_PER_MINUTE": "999999"}, clear=True):
            self.assertLessEqual(settings_from_env()["per_minute"], 600)
        with mock.patch.dict("os.environ", {"SO_MAX_CONCURRENT_AUDITS": "9999"}, clear=True):
            self.assertLessEqual(settings_from_env()["concurrent"], 16)

    def test_burst_never_exceeds_the_steady_rate(self):
        env = {"SO_AUDIT_PER_MINUTE": "2", "SO_AUDIT_BURST": "50"}
        with mock.patch.dict("os.environ", env, clear=True):
            settings = settings_from_env()
        self.assertEqual(settings["burst"], 2)


class TestAuditThrottle(unittest.TestCase):
    def test_burst_is_capped_by_capacity_in_the_object_too(self):
        throttle = AuditThrottle(per_minute=5, burst=99, concurrent=2)
        self.assertEqual(throttle.burst, 5)
        self.assertEqual(throttle.limiter.capacity, 5)

    def test_budget_uses_the_forwarded_key_not_the_raw_text(self):
        throttle = AuditThrottle(per_minute=1, burst=1, concurrent=2)
        self.assertEqual(throttle.budget("203.0.113.9")[0], True)
        # Same real client, different spoofed prefix: still one bucket.
        self.assertEqual(throttle.budget("1.1.1.1, 203.0.113.9")[0], False)
        self.assertEqual(throttle.budget("1.2.3.4, 198.51.100.1")[0], True)

    def test_public_shape_advertises_the_limitations(self):
        self.assertEqual(
            AuditThrottle().as_dict(),
            {
                "per_minute": 6,
                "burst": 3,
                "concurrent": 2,
                "storage": "in-memory",
                "resets_on_restart": True,
            },
        )

    def test_seed_bound_is_a_sane_constant(self):
        self.assertGreaterEqual(MAX_SEED_LENGTH, 20)
        self.assertLessEqual(MAX_SEED_LENGTH, 500)


if __name__ == "__main__":
    unittest.main()
