"""Tests for audit states, limitations and the no-composite-score rule.

Fixture-dependent cases check the rule, not any recorded outcome: the state a
pair receives is re-derived here from its own dimension receipts, so no
Qloo-returned metric is committed and a decision-rule change still fails.
"""

import unittest

from second_opinion.audit import audit_pair, decide_state, rules_payload
from second_opinion.config import AuditConfig, DEFAULT_CONFIG
from second_opinion.evidence import extract_profile

from .helpers import entity, expected_dimension_status, expected_state, needs_captured

FONTAINES = "00000000-0000-4000-8000-0000000000a1"
LITTLE_SIMZ = "00000000-0000-4000-8000-0000000000b2"
BAD_BUNNY = "00000000-0000-4000-8000-0000000000d4"
DUA_LIPA = "00000000-0000-4000-8000-0000000000e5"


def statuses_of(result) -> dict:
    return {name: receipt.status for name, receipt in result.dimensions.items()}


@needs_captured
class TestStates(unittest.TestCase):
    def test_state_follows_the_corroboration_count(self):
        seed, candidate = entity(BAD_BUNNY), entity(DUA_LIPA)
        result = audit_pair(seed, candidate, 0.80)
        self.assertEqual(
            result.state,
            expected_state(
                0.80,
                statuses_of(result),
                extract_profile(seed).is_usable(),
                extract_profile(candidate).is_usable(),
                DEFAULT_CONFIG,
            ),
        )
        if result.state == "CORROBORATED":
            corroborated = [n for n, s in statuses_of(result).items() if s == "corroborated"]
            self.assertGreaterEqual(len(corroborated), DEFAULT_CONFIG.min_dimensions_corroborated)

    def test_each_dimension_status_follows_its_own_overlap(self):
        result = audit_pair(entity(FONTAINES), entity(LITTLE_SIMZ), 0.75)
        self.assertAlmostEqual(result.affinity, 0.75)
        for name, receipt in result.dimensions.items():
            self.assertEqual(
                receipt.status,
                expected_dimension_status(receipt.overlap, DEFAULT_CONFIG),
                f"{name}: overlap {receipt.overlap} reported as {receipt.status}",
            )

    def test_unverifiable_when_candidate_has_no_metadata(self):
        bare = {"entity_id": "NONE", "name": "Bare", "properties": {}, "tags": []}
        result = audit_pair(entity(FONTAINES), bare, 0.75)
        self.assertEqual(result.state, "UNVERIFIABLE")

    def test_unverifiable_when_no_affinity(self):
        result = audit_pair(entity(FONTAINES), entity(LITTLE_SIMZ), None)
        self.assertEqual(result.state, "UNVERIFIABLE")
        self.assertIn("no affinity", result.reason)

    def test_unverifiable_requires_two_evaluable_dimensions(self):
        # genres only, both sides populated
        a = {"entity_id": "A", "name": "A", "properties": {"primary_genres": ["Pop"]}, "tags": []}
        b = {"entity_id": "B", "name": "B", "properties": {"primary_genres": ["Pop"]}, "tags": []}
        result = audit_pair(a, b, 0.9)
        self.assertEqual(result.state, "UNVERIFIABLE")
        self.assertIn("at least 2", result.reason)

    def test_decide_state_is_pure(self):
        statuses = {"genres": "corroborated", "themes": "corroborated",
                    "audience": "weak", "style": "unsupported"}
        state, _ = decide_state(0.9, statuses, True, True, DEFAULT_CONFIG)
        self.assertEqual(state, "CORROBORATED")
        statuses2 = dict(statuses, themes="weak")
        state2, _ = decide_state(0.9, statuses2, True, True, DEFAULT_CONFIG)
        self.assertEqual(state2, "AFFINITY_ONLY")

    def test_threshold_is_configurable(self):
        seed, candidate = entity(BAD_BUNNY), entity(DUA_LIPA)
        strict = AuditConfig(strong_threshold=0.99)
        lenient = AuditConfig(strong_threshold=0.05)
        strict_result = audit_pair(seed, candidate, 0.80, config=strict)
        lenient_result = audit_pair(seed, candidate, 0.80, config=lenient)
        # A stricter threshold cannot corroborate more dimensions, and each state
        # must be the one its own receipts imply.
        self.assertLessEqual(
            sum(1 for s in statuses_of(strict_result).values() if s == "corroborated"),
            sum(1 for s in statuses_of(lenient_result).values() if s == "corroborated"),
        )
        for config, result in ((strict, strict_result), (lenient, lenient_result)):
            self.assertEqual(
                result.state,
                expected_state(
                    0.80,
                    statuses_of(result),
                    extract_profile(seed).is_usable(),
                    extract_profile(candidate).is_usable(),
                    config,
                ),
            )


@needs_captured
class TestAuditShape(unittest.TestCase):
    def setUp(self):
        self.result = audit_pair(entity(FONTAINES), entity(LITTLE_SIMZ), 0.75)

    def test_no_composite_score_field(self):
        payload = self.result.to_dict()
        forbidden = {"score", "confidence", "quality", "composite", "rating"}
        self.assertFalse(forbidden & set(payload))
        for name, receipt in payload["dimensions"].items():
            self.assertFalse(forbidden & set(receipt), f"{name} carries a score")
        # The only aggregate is the discrete state.
        self.assertIn(payload["state"], {"CORROBORATED", "AFFINITY_ONLY", "UNVERIFIABLE"})

    def test_receipts_are_verified(self):
        self.assertTrue(self.result.receipts_verified)
        self.assertEqual(self.result.verification_problems, [])

    def test_limitations_always_state_the_boundary(self):
        joined = " ".join(self.result.limitations).lower()
        self.assertIn("not evidence of shared fans", joined)
        self.assertIn("influenced_by", joined)

    def test_display_only_metadata_is_separate(self):
        self.assertIn("instruments", self.result.display_only)
        self.assertIn("influenced_by", self.result.display_only)
        self.assertNotIn("instruments", self.result.dimensions)

    def test_config_is_reported(self):
        self.assertEqual(self.result.config["strong_threshold"], 0.30)

    def test_rules_payload_documents_every_state(self):
        rules = rules_payload()
        self.assertEqual(set(rules["states"]), {"CORROBORATED", "AFFINITY_ONLY", "UNVERIFIABLE"})
        self.assertEqual(rules["scored_dimensions"], ["genres", "themes", "audience", "style"])


class TestConfigValidation(unittest.TestCase):
    def test_rejects_bad_threshold(self):
        with self.assertRaises(ValueError):
            AuditConfig(strong_threshold=0.0)
        with self.assertRaises(ValueError):
            AuditConfig(weak_threshold=0.9, strong_threshold=0.3)

    def test_rejects_bad_counts(self):
        with self.assertRaises(ValueError):
            AuditConfig(min_dimensions_corroborated=0)
        with self.assertRaises(ValueError):
            AuditConfig(min_dimensions_evaluable=0)


if __name__ == "__main__":
    unittest.main()
