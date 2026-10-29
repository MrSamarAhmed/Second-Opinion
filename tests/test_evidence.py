"""Tests for extraction, normalization and dimension comparison."""

import unittest

from second_opinion.config import AuditConfig, DEFAULT_CONFIG
from second_opinion.evidence import (
    _dedupe,
    compare_dimension,
    compare_profiles,
    extract_profile,
    verify_receipts,
)
from second_opinion.normalize import normalize_term
from second_opinion.schema import Term

from .helpers import entity, load_json, needs_captured, resolve_field


def term(display, field="properties.primary_genres[0]"):
    return Term(display=display, norm=normalize_term(display), field=field)


class TestNormalization(unittest.TestCase):
    def test_case_and_whitespace(self):
        self.assertEqual(normalize_term("  Dance  Pop "), "dance pop")

    def test_hyphen_underscore_slash_unify(self):
        self.assertEqual(normalize_term("Dance-Pop"), "dance pop")
        self.assertEqual(normalize_term("dance_pop"), "dance pop")
        self.assertEqual(normalize_term("R&B"), "r and b")
        self.assertEqual(normalize_term("Hip Hop / Rap"), "hip hop rap")

    def test_punctuation_dropped(self):
        self.assertEqual(normalize_term("D.C. Punk"), "d c punk")

    def test_no_stemming(self):
        # A naive stemmer would merge these; we deliberately do not.
        self.assertNotEqual(normalize_term("Rapping"), normalize_term("Rap"))

    def test_empty_and_diacritics(self):
        self.assertEqual(normalize_term("   "), "")
        self.assertEqual(normalize_term("RÜFÜS"), "rufus")

    def test_non_string_rejected(self):
        with self.assertRaises(TypeError):
            normalize_term(3.5)


@needs_captured
class TestExtraction(unittest.TestCase):
    def test_dimension_sources_match_documented_fields(self):
        profile = extract_profile(entity("00000000-0000-4000-8000-0000000000b2"))  # test pair
        self.assertTrue(profile.dimensions["genres"])
        self.assertTrue(profile.dimensions["themes"])
        self.assertTrue(profile.dimensions["audience"])
        self.assertTrue(profile.dimensions["style"])
        for t in profile.dimensions["genres"]:
            self.assertTrue(t.field.startswith("properties.primary_genres[")
                            or t.field.startswith("properties.secondary_genres["))
        for t in profile.dimensions["audience"]:
            self.assertTrue(t.field.startswith("tags[urn:tag:audience:qloo:"))
        for t in profile.dimensions["style"]:
            self.assertTrue(t.field.startswith("tags[urn:tag:style:qloo:"))

    def test_genres_include_primary_and_secondary(self):
        payload = entity("00000000-0000-4000-8000-0000000000b2")
        profile = extract_profile(payload)
        fields = {t.field.split("[")[0] for t in profile.dimensions["genres"]}
        self.assertEqual(fields, {"properties.primary_genres", "properties.secondary_genres"})

    def test_display_only_dimensions_are_not_scored(self):
        profile = extract_profile(entity("00000000-0000-4000-8000-0000000000b2"))
        self.assertIn("instruments", profile.display_only)
        self.assertIn("influenced_by", profile.display_only)
        self.assertNotIn("instruments", profile.dimensions)
        self.assertNotIn("influenced_by", profile.dimensions)

    def test_missing_metadata_is_recorded_not_invented(self):
        sparse = {"entity_id": "X", "name": "Empty", "properties": {}, "tags": []}
        profile = extract_profile(sparse)
        self.assertFalse(profile.is_usable())
        self.assertEqual(sorted(profile.missing), ["audience", "genres", "style", "themes"])
        for dim in ("genres", "themes", "audience", "style"):
            self.assertEqual(profile.dimensions[dim], [])

    def test_payload_without_id_raises(self):
        with self.assertRaises(ValueError):
            extract_profile({"name": "No id"})

    def test_non_mapping_raises(self):
        with self.assertRaises(ValueError):
            extract_profile(["not", "a", "mapping"])

    def test_tags_missing_name_are_skipped(self):
        payload = {
            "entity_id": "Y", "name": "Broken",
            "properties": {},
            "tags": [
                {"type": "urn:tag:audience:qloo"},              # no name
                {"type": "urn:tag:audience:qloo", "name": "Loyal", "id": "urn:tag:audience:qloo:loyal"},
            ],
        }
        profile = extract_profile(payload)
        self.assertEqual([t.display for t in profile.dimensions["audience"]], ["Loyal"])


class TestDuplicates(unittest.TestCase):
    def test_duplicate_tags_collapse(self):
        payload = {
            "entity_id": "Z", "name": "Dup",
            "properties": {"primary_genres": ["Pop", "pop", " POP "]},
            "tags": [
                {"type": "urn:tag:audience:qloo", "name": "Loyal", "id": "urn:tag:audience:qloo:loyal"},
                {"type": "urn:tag:audience:qloo", "name": "loyal", "id": "urn:tag:audience:qloo:loyal"},
            ],
        }
        profile = extract_profile(payload)
        self.assertEqual(len(profile.dimensions["genres"]), 1)
        self.assertEqual(len(profile.dimensions["audience"]), 1)

    def test_dedupe_counts_collapsed(self):
        terms, collapsed = _dedupe([term("Pop"), term("pop"), term("Rock")])
        self.assertEqual([t.display for t in terms], ["Pop", "Rock"])
        self.assertEqual(collapsed, 1)

    def test_duplicates_do_not_change_overlap(self):
        a = extract_profile({"entity_id": "A", "name": "A",
                             "properties": {"primary_genres": ["Pop", "Pop"]}, "tags": []})
        b = extract_profile({"entity_id": "B", "name": "B",
                             "properties": {"primary_genres": ["Pop"]}, "tags": []})
        result = compare_profiles(a, b, DEFAULT_CONFIG)["genres"]
        self.assertEqual(result.overlap, 1.0)
        self.assertEqual(result.duplicates_collapsed, 1)


class TestComparison(unittest.TestCase):
    def test_overlap_uses_min_denominator(self):
        # 2 shared of min(2, 5) -> 1.0, not Jaccard 2/5.
        a = [term("Pop", "properties.primary_genres[0]"), term("Rock", "properties.primary_genres[1]")]
        b = [term("Pop", "properties.primary_genres[0]"), term("Rock", "properties.primary_genres[1]"),
             term("Jazz", "properties.primary_genres[2]"), term("Folk", "properties.primary_genres[3]"),
             term("Blues", "properties.primary_genres[4]")]
        result = compare_dimension("genres", a, b, DEFAULT_CONFIG)
        self.assertAlmostEqual(result.overlap, 1.0)
        self.assertEqual(result.status, "corroborated")

    def test_status_threshold_boundaries(self):
        config = AuditConfig(strong_threshold=0.30)
        # exactly at threshold -> corroborated
        a = [term("X"), term("Y"), term("Z"), term("W")]
        b = [term("X"), term("Q")]
        self.assertAlmostEqual(compare_dimension("genres", a, b, config).overlap, 0.5)
        self.assertEqual(compare_dimension("genres", a, b, config).status, "corroborated")
        # just below threshold -> weak
        c = [term(f"t{i}") for i in range(10)]
        d = [term("t0"), term("zzz")]
        # min(|c|,|d|) = 2, shared = 1 -> 0.5 ... use a case that lands at 0.25
        e = [term("a"), term("b"), term("c"), term("d")]
        f = [term("a"), term("q"), term("r"), term("s")]
        self.assertEqual(compare_dimension("genres", e, f, config).status, "weak")
        # zero overlap -> unsupported
        self.assertEqual(compare_dimension("genres", e, f[1:], config).status, "unsupported")

    def test_one_sided_data_is_unverifiable(self):
        a = [term("Pop")]
        result = compare_dimension("genres", a, [], DEFAULT_CONFIG)
        self.assertEqual(result.status, "unverifiable")
        self.assertIsNone(result.overlap)
        self.assertIn("candidate", result.reason)

    def test_both_sides_empty_is_unverifiable(self):
        result = compare_dimension("style", [], [], DEFAULT_CONFIG)
        self.assertEqual(result.status, "unverifiable")
        self.assertIn("neither", result.reason)


@needs_captured
class TestComparableDimensions(unittest.TestCase):
    def test_comparable_dimensions_all_four(self):
        seed = extract_profile(entity("00000000-0000-4000-8000-0000000000a1"))
        cand = extract_profile(entity("00000000-0000-4000-8000-0000000000b2"))
        dims = compare_profiles(seed, cand, DEFAULT_CONFIG)
        self.assertEqual(sorted(dims), ["audience", "genres", "style", "themes"])
        self.assertEqual(dims["audience"].status, "corroborated")


@needs_captured
class TestProvenance(unittest.TestCase):
    def test_every_receipt_resolves_in_both_payloads(self):
        seed_payload = entity("00000000-0000-4000-8000-0000000000a1")
        cand_payload = entity("00000000-0000-4000-8000-0000000000b2")
        seed, cand = extract_profile(seed_payload), extract_profile(cand_payload)
        dims = compare_profiles(seed, cand, DEFAULT_CONFIG)

        checked = 0
        for name, receipt in dims.items():
            for shared, seed_path, cand_path in zip(
                receipt.shared_terms, receipt.seed_sources, receipt.candidate_sources
            ):
                self.assertEqual(
                    normalize_term(resolve_field(seed_payload, seed_path)),
                    normalize_term(shared),
                    f"{name}: seed provenance {seed_path} does not hold {shared!r}",
                )
                self.assertEqual(
                    normalize_term(resolve_field(cand_payload, cand_path)),
                    normalize_term(shared),
                    f"{name}: candidate provenance {cand_path} does not hold {shared!r}",
                )
                checked += 1
        self.assertGreater(checked, 0)

    def test_fabricated_shared_term_is_caught(self):
        seed_payload = entity("00000000-0000-4000-8000-0000000000a1")
        cand_payload = entity("00000000-0000-4000-8000-0000000000b2")
        seed, cand = extract_profile(seed_payload), extract_profile(cand_payload)
        dims = compare_profiles(seed, cand, DEFAULT_CONFIG)

        # Inject a plausible-but-absent term into the genres receipt.
        broken = dims["genres"]
        broken.shared_terms.append("Shoegaze")
        broken.seed_sources.append("properties.primary_genres[0]")
        broken.candidate_sources.append("properties.primary_genres[0]")

        class Shim:
            dimensions = dims

        problems = verify_receipts(seed_payload, cand_payload, Shim())
        self.assertTrue(problems)
        self.assertIn("genres", problems[0])

    def test_verification_passes_on_real_data(self):
        seed_payload = entity("00000000-0000-4000-8000-0000000000a1")
        cand_payload = entity("00000000-0000-4000-8000-0000000000b2")
        seed, cand = extract_profile(seed_payload), extract_profile(cand_payload)
        dims = compare_profiles(seed, cand, DEFAULT_CONFIG)

        class Shim:
            dimensions = dims

        self.assertEqual(verify_receipts(seed_payload, cand_payload, Shim()), [])


if __name__ == "__main__":
    unittest.main()
