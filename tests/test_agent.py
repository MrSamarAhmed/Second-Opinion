"""Tests for the agent state machine: trace, refusals, and the void-audit path."""

import unittest
from unittest import mock

from second_opinion.adapters import AdapterError, EntityResolutionError, FixtureAdapter, SeedRef
from second_opinion.agent import SecondOpinionAgent
from second_opinion.audit import audit_pair
from second_opinion.config import AuditConfig, DEFAULT_CONFIG
from second_opinion.evidence import extract_profile

from .helpers import captured_id, entity, expected_state, needs_captured, resolve_field

FONTAINES = "00000000-0000-4000-8000-0000000000a1"
LITTLE_SIMZ = "00000000-0000-4000-8000-0000000000b2"
RUFUS = "00000000-0000-4000-8000-0000000000c3"


class StubAdapter:
    """Minimal adapter for exercising failure branches without fixtures.

    `mode` is deliberately "stub", so a test that accidentally reaches the
    network would show up as an unknown mode rather than as fixture success.
    """

    mode = "stub"

    def __init__(self, seeds=None, candidates=None, payload=None, raise_on=None):
        self._seeds = seeds or {}
        self._candidates = candidates or {}
        self._payload = payload or {}
        self._raise_on = raise_on or {}

    def resolve_seed(self, query):
        if "resolve" in self._raise_on:
            raise self._raise_on["resolve"]
        if query not in self._seeds:
            raise EntityResolutionError(f"unknown seed {query!r}")
        return SeedRef(query, self._seeds[query])

    def candidates(self, seed_id):
        if "candidates" in self._raise_on:
            raise self._raise_on["candidates"]
        return self._candidates.get(seed_id, [])

    def seed_payload(self, seed_id):
        return self._payload.get(seed_id)


@needs_captured
class TestFixtureWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = SecondOpinionAgent(FixtureAdapter()).audit("Fontaines D.C.")

    def test_successful_audit_reports_mode_and_seed(self):
        self.assertTrue(self.report["ok"])
        self.assertIsNone(self.report["refusal"])
        self.assertEqual(self.report["mode"], "fixture")
        # The id is read from the local captures at runtime: no API-returned
        # identifier is written into this repository.
        self.assertEqual(self.report["seed"]["entity_id"], captured_id(FONTAINES))
        self.assertTrue(self.report["seed"]["usable"])

    def test_trace_records_the_documented_steps_in_order(self):
        steps = [entry["step"] for entry in self.report["trace"]]
        self.assertEqual(steps[0], "resolve_seed")
        self.assertEqual(steps[1], "retrieve_candidates")
        self.assertEqual(steps[2], "extract_metadata")
        self.assertIn("verify_receipts", steps)
        self.assertEqual(steps[-1], "decide")
        for entry in self.report["trace"]:
            self.assertIn(entry["status"], {"ok", "failed", "stopped"})

    def test_every_candidate_carries_a_verified_receipt(self):
        self.assertTrue(self.report["candidates"])
        for candidate in self.report["candidates"]:
            self.assertTrue(candidate["receipts_verified"], candidate["candidate"])
            self.assertEqual(candidate["verification_problems"], [])
            self.assertIn(
                candidate["state"], {"CORROBORATED", "AFFINITY_ONLY", "UNVERIFIABLE"}
            )

    def test_receipt_sources_resolve_in_the_seed_payload(self):
        """Provenance must point at a real field in the stored seed record."""
        seed_payload = entity(FONTAINES)
        checked = 0
        for candidate in self.report["candidates"]:
            for receipt in candidate["dimensions"].values():
                for path in receipt["seed_sources"]:
                    resolve_field(seed_payload, path)
                    checked += 1
        self.assertGreater(checked, 0, "no seed provenance was exercised")

    def test_a_saved_candidate_state_follows_its_own_receipts(self):
        hit = next(
            c for c in self.report["candidates"] if c["candidate_id"] == captured_id(LITTLE_SIMZ)
        )
        self.assertEqual(
            hit["state"],
            expected_state(
                hit["affinity"],
                {name: r["status"] for name, r in hit["dimensions"].items()},
                self.report["seed"]["usable"],
                extract_profile(entity(LITTLE_SIMZ)).is_usable(),
                DEFAULT_CONFIG,
            ),
        )
        # Affinity is reported as it arrived, in its documented range.
        self.assertIsInstance(hit["affinity"], float)
        self.assertTrue(0.0 <= hit["affinity"] <= 1.0, hit["affinity"])

    def test_dimension_summary_is_present_and_consistent(self):
        summary = self.report["dimension_summary"]
        self.assertEqual(set(summary), {"genres", "themes", "audience", "style"})
        total = len(self.report["candidates"])
        for name, counts in summary.items():
            self.assertEqual(counts["total"], total)
            observed = sum(
                counts[s] for s in ("corroborated", "weak", "unsupported", "unverifiable")
            )
            self.assertEqual(observed, total, name)
            self.assertEqual(counts["is_blanket"], total > 1 and counts["corroborated"] == total)

    def test_limit_restricts_candidate_count(self):
        report = SecondOpinionAgent(FixtureAdapter()).audit("Fontaines D.C.", limit=2)
        self.assertEqual(len(report["candidates"]), 2)


@needs_captured
class TestRefusals(unittest.TestCase):
    def test_seed_without_metadata_refuses(self):
        report = SecondOpinionAgent(FixtureAdapter()).audit("RÜFÜS DU SOL")
        self.assertFalse(report["ok"])
        self.assertIsNone(report["seed"])
        refusal = report["refusal"]
        self.assertEqual(refusal["code"], "insufficient seed metadata")
        seed_id = captured_id(RUFUS)
        self.assertEqual(refusal["seed_id"], seed_id)
        # Every number on the refusal card is recomputed here from the same
        # source the agent read, rather than copied from a recorded result.
        saved = FixtureAdapter()
        candidates = saved.candidates(seed_id)
        self.assertEqual(refusal["facts"]["candidate_count"], len(candidates))
        self.assertEqual(
            refusal["facts"]["affinities_present"],
            sum(1 for _, affinity in candidates if affinity is not None),
        )
        self.assertEqual(refusal["facts"]["seed_tag_count"], len(entity(RUFUS).get("tags") or []))
        self.assertEqual(refusal["facts"]["seed_tag_count"], 0)
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["trace"][-1]["step"], "refuse")

    def test_unknown_seed_is_a_resolution_refusal(self):
        report = SecondOpinionAgent(FixtureAdapter()).audit("Nobody At All")
        self.assertFalse(report["ok"])
        self.assertEqual(report["refusal"]["code"], "seed resolution failed")
        self.assertIsNone(report["refusal"]["seed_id"])

    def test_resolution_adapter_error_is_a_refusal_not_a_crash(self):
        adapter = StubAdapter(raise_on={"resolve": AdapterError("upstream down")})
        report = SecondOpinionAgent(adapter).audit("anything")
        self.assertFalse(report["ok"])
        self.assertEqual(report["refusal"]["code"], "seed resolution failed")
        self.assertIn("upstream down", report["refusal"]["detail"])

    def test_candidate_retrieval_failure_is_a_refusal(self):
        adapter = StubAdapter(
            seeds={"x": "X"}, raise_on={"candidates": EntityResolutionError("no set")}
        )
        report = SecondOpinionAgent(adapter).audit("x")
        self.assertFalse(report["ok"])
        self.assertEqual(report["refusal"]["code"], "candidate retrieval failed")
        self.assertEqual(report["refusal"]["seed_id"], "x")

    def test_authentication_failure_has_its_own_refusal_code(self):
        from second_opinion.adapters import AuthenticationError

        adapter = StubAdapter(raise_on={"resolve": AuthenticationError("HTTP 401")})
        report = SecondOpinionAgent(adapter).audit("x")
        self.assertFalse(report["ok"])
        self.assertEqual(report["refusal"]["code"], "authentication failed")
        self.assertIn("HTTP 401", report["refusal"]["detail"])

    def test_missing_seed_payload_is_a_refusal(self):
        adapter = StubAdapter(seeds={"x": "X"})
        report = SecondOpinionAgent(adapter).audit("x")
        self.assertFalse(report["ok"])
        self.assertEqual(report["refusal"]["code"], "seed metadata missing")

    def test_malformed_seed_payload_is_a_refusal(self):
        adapter = StubAdapter(seeds={"x": "X"}, payload={"x": {"name": "no id"}})
        report = SecondOpinionAgent(adapter).audit("x")
        self.assertFalse(report["ok"])
        self.assertEqual(report["refusal"]["code"], "seed metadata malformed")


@needs_captured
class TestReceiptVerificationFailure(unittest.TestCase):
    def test_a_tampered_receipt_voids_the_audit(self):
        """If verification fails the candidate must drop to UNVERIFIABLE."""
        good = audit_pair(entity(FONTAINES), entity(LITTLE_SIMZ), 0.75)
        tampered = type(good)(
            seed=good.seed,
            seed_id=good.seed_id,
            candidate=good.candidate,
            candidate_id=good.candidate_id,
            affinity=good.affinity,
            state="CORROBORATED",
            reason="fabricated",
            dimensions=good.dimensions,
            display_only=good.display_only,
            limitations=good.limitations,
            receipts_verified=False,
            verification_problems=["genres: reported shared terms do not match payload"],
            config=good.config,
        )
        with mock.patch("second_opinion.agent.audit_pair", return_value=tampered):
            report = SecondOpinionAgent(FixtureAdapter()).audit("Fontaines D.C.", limit=1)
        candidate = report["candidates"][0]
        self.assertEqual(candidate["state"], "UNVERIFIABLE")
        self.assertIn("verification failed", candidate["reason"].lower())
        failed = [e for e in report["trace"] if e["step"] == "verify_receipts"]
        self.assertTrue(failed and failed[0]["status"] == "failed")

    def test_unreadable_candidate_is_marked_unverifiable_not_skipped(self):
        seed = entity(FONTAINES)
        adapter = StubAdapter(
            seeds={"x": "X"},
            payload={"x": seed},
            candidates={"x": [({"name": "Broken"}, 0.5)]},
        )
        report = SecondOpinionAgent(adapter).audit("x")
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual(report["candidates"][0]["state"], "UNVERIFIABLE")


@needs_captured
class TestNoUnsupportedClaims(unittest.TestCase):
    BANNED = (
        "shared fan", "proof", "likely", "probably", "will appeal",
        "attendance", "revenue", "ticket",
    )

    def test_receipt_reasons_make_no_behavioural_claims(self):
        report = SecondOpinionAgent(FixtureAdapter()).audit("Bad Bunny")
        for candidate in report["candidates"]:
            texts = [candidate["reason"]]
            texts += [r["reason"] for r in candidate["dimensions"].values()]
            for text in texts:
                lowered = text.lower()
                for phrase in self.BANNED:
                    self.assertNotIn(phrase, lowered, f"{candidate['candidate']}: {text}")

    def test_boundary_is_stated_on_every_candidate(self):
        report = SecondOpinionAgent(FixtureAdapter()).audit("Bad Bunny")
        for candidate in report["candidates"]:
            joined = " ".join(candidate["limitations"]).lower()
            self.assertIn("not evidence of shared fans", joined)

    def test_custom_config_is_reported_through_the_agent(self):
        report = SecondOpinionAgent(
            FixtureAdapter(), config=AuditConfig(strong_threshold=0.45)
        ).audit("Bad Bunny", limit=1)
        self.assertEqual(report["rules"]["thresholds"]["strong_threshold"], 0.45)
        self.assertEqual(report["candidates"][0]["config"]["strong_threshold"], 0.45)


if __name__ == "__main__":
    unittest.main()
