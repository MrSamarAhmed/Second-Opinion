"""The bounded investigation pass: one gap-triggered re-read, judged by the same rules.

After the first pass the agent looks for a verdict that rests on a *one-sided*
gap — a dimension where the seed carries terms and the record in hand for the
candidate carries none, which is a limit of how the entity was fetched rather
than a finding about the music. It may then spend one follow-up lookup of that
entity's own record and re-run the identical audit and receipt rules over
whatever comes back.

Everything in this file runs on a clean checkout: no captured Qloo response, no
credential, no network. The records are invented here (and in
`second_opinion.demo.py`), while the extraction, arithmetic, state rule, receipt
re-derivation and the pass itself are the production code paths.

The load-bearing assertions are call counts. The adapters below record every
lookup, so "at most one follow-up" is proven by counting what was asked for, not
by trusting a flag the agent sets about itself. The verdict assertions are
checked against hand-computed overlaps and the independent state oracle in
`tests/helpers.py`, not against a recorded expected value.
"""

import io
import json
import os
import threading
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from unittest import mock

from second_opinion import agent as agent_module
from second_opinion.adapters import (
    AdapterError,
    AuthenticationError,
    EntityResolutionError,
    SeedRef,
    UpstreamTimeout,
)
from second_opinion.agent import MAX_FOLLOW_UP_QUERIES, SecondOpinionAgent
from second_opinion.audit import audit_pair
from second_opinion.config import DEFAULT_CONFIG
from second_opinion.demo import (
    AFFINITIES,
    DEMO_SEED,
    DIRECT_RECORDS,
    LIST_RECORDS,
    ORDER,
    SEED_ID,
    SEED_RECORD,
    SyntheticAdapter,
)
from second_opinion.throttle import AuditThrottle, set_default_throttle

from .helpers import expected_state, resolve_field
from .test_rate_limit import _QuietHandler, _request

# ---------------------------------------------------------------- invented data
#
# Shapes follow the documented payload structure only: `entity_id`, `name`,
# `properties.*` lists, `tags[]` with a `type` and an `id`. Nothing here is a
# real artist, a real term returned by Qloo, or a captured response.

# A stand-in credential: any test that puts one where the agent could reach it
# asserts that it never comes back out in a report.
SENTINEL = "super-secret-key-that-must-never-leak"

SEED_ID_LOCAL = "t-seed"


def rec(entity_id, name, genres=(), secondary=(), themes=(), audience=(), style=()):
    tags = []
    tags.extend({"id": f"aud-{i}", "type": "urn:tag:audience:qloo", "name": term}
                for i, term in enumerate(audience, start=1))
    tags.extend({"id": f"sty-{i}", "type": "urn:tag:style:qloo", "name": term}
                for i, term in enumerate(style, start=1))
    return {
        "entity_id": entity_id,
        "name": name,
        "properties": {
            "primary_genres": list(genres),
            "secondary_genres": list(secondary),
            "lyrical_themes": list(themes),
            "instruments": [],
            "influenced_by": [],
        },
        "tags": tags,
    }


# The seed speaks in all four dimensions, so a candidate that says nothing in one
# of them is silent on its own side and the gap is one-sided by construction.
SEED = rec(
    SEED_ID_LOCAL, "Test Seed",
    genres=("indie folk",), secondary=("chamber pop",),
    themes=("longing", "night travel"),
    audience=("folk traditionalists",), style=("intimate",),
)

# In the recommendation list: genres {indie folk} -> overlap 1/1 = 1.000
# corroborated; themes {longing, harbour life} vs {longing, night travel} ->
# 1/2 = 0.500 corroborated; audience and style empty -> unverifiable, one-sided.
# Two corroborated of two evaluable meets the rule, so the first-pass verdict is
# CORROBORATED and this candidate must NOT be investigated.
RICH_LIST = rec("t-rich", "Rich Record", genres=("indie folk",),
                themes=("longing", "harbour life"))

# List record: genres {sea shanty} vs seed {indie folk, chamber pop} -> 0/1 =
# 0.000 unsupported; themes {harbours} vs {longing, night travel} -> 0/1 = 0.000
# unsupported; audience and style empty -> unverifiable, one-sided. Evaluable 2,
# corroborated 0 -> AFFINITY_ONLY, and the gap is real doubt about the fetch.
THIN_LIST = rec("t-thin", "Thin Record", genres=("sea shanty",), themes=("harbours",))

# The by-id re-read of the same entity: it gains audience and style terms that
# intersect the seed, so two more dimensions become comparable and the verdict
# moves on evidence, not on a new score.
THIN_DIRECT = rec(
    "t-thin", "Thin Record",
    genres=("sea shanty",), themes=("harbours",),
    audience=("folk traditionalists",), style=("intimate",),
)

# The by-id re-read that adds terms which intersect nothing: audience
# {arena pop listeners} against the seed's {folk traditionalists} is 0/1 = 0.000
# and style {stadium} against {intimate} is 0/1 = 0.000. More evidence arrives;
# the verdict does not improve.
THIN_DIRECT_DISJOINT = rec(
    "t-thin", "Thin Record",
    genres=("sea shanty",), themes=("harbours",),
    audience=("arena pop listeners",), style=("stadium",),
)

# A candidate that is silent in three dimensions: only genres is evaluable, so
# the rule cannot be met and the verdict is UNVERIFIABLE before and after.
SPARSE_LIST = rec("t-sparse", "Sparse Record", genres=("sea shanty",))

# The by-id record that says *less* than the list copy did (themes vanish). The
# direct record is the better evidence about the entity, so the verdict moves
# down. This is the third honest outcome of a follow-up.
THIN_DIRECT_SPARSE = rec("t-thin", "Thin Record", genres=("sea shanty",))


class ScriptedAdapter:
    """The three adapter calls, with every lookup recorded for counting."""

    mode = "synthetic"

    def __init__(self, candidates=None, direct=None, fail=None, missing=False,
                 corrupt=False):
        self.candidate_list = list(candidates or [])
        self.direct = direct or {}
        self.fail = fail
        self.missing = missing
        self.corrupt = corrupt
        self.lookups = []

    # -- the calls the agent is allowed to make --------------------------------
    def resolve_seed(self, query):
        self.lookups.append(("resolve_seed", query))
        return SeedRef(SEED_ID_LOCAL, query)

    def candidates(self, seed_id):
        self.lookups.append(("candidates", seed_id))
        return list(self.candidate_list)

    def seed_payload(self, entity_id):
        if entity_id == SEED_ID_LOCAL:
            self.lookups.append(("seed_record", entity_id))
            return SEED
        self.lookups.append(("entity_record", entity_id))
        if self.fail is not None:
            raise self.fail
        if self.missing:
            return None
        if self.corrupt:
            return {"name": "this payload has no entity_id"}
        return self.direct.get(entity_id)

    # -- what the tests count ---------------------------------------------------
    def entity_lookups(self):
        return [ident for kind, ident in self.lookups if kind == "entity_record"]


def run(adapter, query="Test Seed", **kwargs):
    """One audit, with the agent's follow-up budget left at its default."""
    return SecondOpinionAgent(adapter, **kwargs).audit(query)


class TestGapDetection(unittest.TestCase):
    """The pass is triggered by missing evidence, never by curiosity."""

    def test_no_one_sided_gap_means_no_further_lookup(self):
        adapter = ScriptedAdapter(candidates=[(RICH_LIST, 0.90)])
        report = run(adapter)
        self.assertTrue(report["ok"])
        self.assertEqual(adapter.entity_lookups(), [])
        self.assertEqual(report["seed"]["entity_id"], SEED_ID_LOCAL)
        investigation = report["investigation"]
        self.assertFalse(investigation["triggered"])
        self.assertEqual(investigation["follow_up_queries_used"], 0)
        self.assertEqual(investigation["candidates_with_gap"], 0)
        self.assertIsNone(investigation["gap"])
        # The candidate keeps its own verdict: CORROBORATED on the first pass.
        self.assertEqual(report["candidates"][0]["state"], "CORROBORATED")
        gap_step = next(s for s in report["trace"] if s["step"] == "identify_gap")
        self.assertEqual(gap_step["status"], "ok")
        self.assertIn("no follow-up warranted", gap_step["detail"])
        self.assertNotIn("follow_up_query", [s["step"] for s in report["trace"]])

    def test_a_gap_is_named_by_candidate_dimension_and_verdict(self):
        adapter = ScriptedAdapter(candidates=[(RICH_LIST, 0.90), (THIN_LIST, 0.80)])
        report = run(adapter)
        gap = report["investigation"]["gap"]
        self.assertEqual(gap["candidate"], "Thin Record")
        self.assertEqual(sorted(gap["dimensions"]), ["audience", "style"])
        self.assertEqual(gap["state"], "AFFINITY_ONLY")
        self.assertEqual(gap["affinity"], 0.80)
        # Only the candidate whose verdict is actually in doubt counts as a gap.
        self.assertEqual(report["investigation"]["candidates_with_gap"], 1)

    def test_a_candidate_with_no_affinity_is_not_investigated(self):
        """No affinity means no recommendation, and no record can supply one."""
        adapter = ScriptedAdapter(candidates=[(THIN_LIST, None)])
        report = run(adapter)
        self.assertEqual(adapter.entity_lookups(), [])
        self.assertEqual(report["investigation"]["candidates_with_gap"], 0)
        candidate = report["candidates"][0]
        self.assertEqual(candidate["state"], "UNVERIFIABLE")
        self.assertIn("no affinity", candidate["reason"])

    def test_a_void_audit_is_not_treated_as_a_gap(self):
        """Failed receipts are the strongest negative; a re-read is not the remedy."""
        broken = replace(
            audit_pair(SEED, THIN_LIST, 0.80),
            state="UNVERIFIABLE",
            receipts_verified=False,
            verification_problems=["genres: reported shared terms do not match payload"],
        )
        adapter = ScriptedAdapter(candidates=[(THIN_LIST, 0.80)])
        with mock.patch("second_opinion.agent.audit_pair", return_value=broken):
            report = run(adapter)
        self.assertEqual(adapter.entity_lookups(), [])
        self.assertEqual(report["investigation"]["candidates_with_gap"], 0)
        self.assertFalse(report["candidates"][0]["receipts_verified"])
        self.assertEqual(report["candidates"][0]["state"], "UNVERIFIABLE")

    def test_a_corroborated_candidate_is_never_re_read(self):
        """The pass may only add doubt or resolve it, never demote a settled verdict."""
        adapter = ScriptedAdapter(candidates=[(RICH_LIST, 0.95)])
        report = run(adapter)
        self.assertEqual(report["candidates"][0]["state"], "CORROBORATED")
        self.assertNotIn("investigation", report["candidates"][0])
        self.assertEqual(adapter.entity_lookups(), [])


class TestFollowUpBudget(unittest.TestCase):
    """One lookup per audit, enforced at the code level and on failure paths too."""

    def test_two_gaps_still_cost_exactly_one_lookup(self):
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80), (SPARSE_LIST, 0.70)],
            direct={"t-thin": THIN_DIRECT, "t-sparse": SPARSE_LIST},
        )
        report = run(adapter)
        self.assertEqual(len(adapter.entity_lookups()), 1)
        self.assertEqual(report["investigation"]["follow_up_queries_used"], 1)
        self.assertEqual(report["investigation"]["candidates_with_gap"], 2)
        # The highest-affinity gap is chosen: 0.80 over 0.70.
        self.assertEqual(adapter.entity_lookups(), ["t-thin"])

    def test_the_lookup_is_only_ever_an_id_already_in_the_candidate_set(self):
        """The follow-up re-reads a recommendation; it never fishes for new ones."""
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80)], direct={"t-thin": THIN_DIRECT}
        )
        report = run(adapter)
        allowed = {record["entity_id"] for record, _ in adapter.candidate_list}
        self.assertTrue(set(adapter.entity_lookups()) <= allowed)
        self.assertEqual(
            [kind for kind, _ in adapter.lookups],
            ["resolve_seed", "candidates", "seed_record", "entity_record"],
        )

    def test_a_caller_cannot_raise_the_ceiling(self):
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80), (SPARSE_LIST, 0.70)],
            direct={"t-thin": THIN_DIRECT, "t-sparse": SPARSE_LIST},
        )
        report = run(adapter, max_follow_up_queries=99)
        self.assertEqual(MAX_FOLLOW_UP_QUERIES, 1)
        self.assertEqual(report["investigation"]["max_follow_up_queries"], 1)
        self.assertEqual(len(adapter.entity_lookups()), 1)

    def test_the_budget_can_be_lowered_to_zero(self):
        adapter = ScriptedAdapter(candidates=[(THIN_LIST, 0.80)])
        report = run(adapter, max_follow_up_queries=0)
        self.assertEqual(adapter.entity_lookups(), [])
        self.assertEqual(report["investigation"]["decision"].count("budget"), 1)
        step = next(s for s in report["trace"] if s["step"] == "plan_follow_up")
        self.assertEqual(step["status"], "stopped")
        self.assertIn("budget", step["detail"])

    def test_a_negative_budget_is_clamped_not_honoured(self):
        adapter = ScriptedAdapter(candidates=[(THIN_LIST, 0.80)])
        report = run(adapter, max_follow_up_queries=-5)
        self.assertEqual(report["investigation"]["max_follow_up_queries"], 0)
        self.assertEqual(adapter.entity_lookups(), [])

    def test_a_failed_lookup_still_costs_the_budget(self):
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80), (SPARSE_LIST, 0.70)],
            fail=UpstreamTimeout("the follow-up request timed out"),
        )
        report = run(adapter)
        self.assertEqual(len(adapter.entity_lookups()), 1, "no retry after a failure")
        self.assertEqual(report["investigation"]["follow_up_queries_used"], 1)
        self.assertFalse(report["investigation"]["outcome"]["changed"])

    def test_a_second_audit_on_the_same_agent_gets_its_own_budget(self):
        """The counter is per audit: a reused agent must not carry spend forward."""
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80)], direct={"t-thin": THIN_DIRECT}
        )
        agent = SecondOpinionAgent(adapter)
        first = agent.audit("Test Seed")
        second = agent.audit("Test Seed")
        self.assertEqual(first["investigation"]["follow_up_queries_used"], 1)
        self.assertEqual(second["investigation"]["follow_up_queries_used"], 1)
        self.assertEqual(len(adapter.entity_lookups()), 2)


class TestVerdictChanges(unittest.TestCase):
    """A revised verdict must be justified by arithmetic, not asserted by the agent."""

    def test_a_re_read_that_populates_dimensions_can_improve_the_verdict(self):
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80)], direct={"t-thin": THIN_DIRECT}
        )
        report = run(adapter)
        candidate = report["candidates"][0]
        dims = candidate["dimensions"]

        # Hand-computed overlap coefficients, |A ∩ B| / min(|A|,|B|):
        #   genres  {sea shanty} vs {indie folk, chamber pop}   -> 0/1 = 0.000
        #   themes  {harbours} vs {longing, night travel}        -> 0/1 = 0.000
        #   audience {folk traditionalists} vs {folk traditionalists} -> 1/1 = 1.000
        #   style   {intimate} vs {intimate}                     -> 1/1 = 1.000
        self.assertAlmostEqual(dims["genres"]["overlap"], 0.0)
        self.assertAlmostEqual(dims["themes"]["overlap"], 0.0)
        self.assertAlmostEqual(dims["audience"]["overlap"], 1.0)
        self.assertAlmostEqual(dims["style"]["overlap"], 1.0)
        statuses = {name: receipt["status"] for name, receipt in dims.items()}
        self.assertEqual(
            statuses,
            {"genres": "unsupported", "themes": "unsupported",
             "audience": "corroborated", "style": "corroborated"},
        )
        # Independent restatement of the rule, from the README rather than from
        # the code under test: 4 evaluable, 2 corroborated >= 2 -> CORROBORATED.
        self.assertEqual(
            candidate["state"],
            expected_state(0.80, statuses, seed_usable=True, candidate_usable=True),
        )
        self.assertEqual(candidate["state"], "CORROBORATED")
        self.assertTrue(candidate["receipts_verified"])

        investigation = report["investigation"]
        self.assertEqual(investigation["outcome"]["state_before"], "AFFINITY_ONLY")
        self.assertEqual(investigation["outcome"]["state_after"], "CORROBORATED")
        self.assertTrue(investigation["outcome"]["changed"])
        self.assertEqual(sorted(investigation["outcome"]["newly_comparable"]),
                         ["audience", "style"])
        self.assertEqual(investigation["outcome"]["remaining_gaps"], [])
        self.assertEqual(candidate["investigation"]["state_before"], "AFFINITY_ONLY")

    def test_extra_evidence_alone_does_not_upgrade_an_unsupported_verdict(self):
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80)], direct={"t-thin": THIN_DIRECT_DISJOINT}
        )
        report = run(adapter)
        candidate = report["candidates"][0]
        dims = candidate["dimensions"]
        # Every dimension is now comparable and every overlap is zero.
        self.assertEqual({d: dims[d]["status"] for d in dims},
                         {"genres": "unsupported", "themes": "unsupported",
                          "audience": "unsupported", "style": "unsupported"})
        self.assertEqual(candidate["state"], "AFFINITY_ONLY")
        outcome = report["investigation"]["outcome"]
        self.assertFalse(outcome["changed"])
        self.assertEqual(sorted(outcome["newly_comparable"]), ["audience", "style"])
        self.assertIn("verdict unchanged", report["investigation"]["decision"])

    def test_a_re_read_can_carry_less_and_the_verdict_moves_down(self):
        """The third honest outcome: a direct record that supports less than the list copy."""
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80)], direct={"t-thin": THIN_DIRECT_SPARSE}
        )
        report = run(adapter)
        candidate = report["candidates"][0]
        self.assertEqual(candidate["state"], "UNVERIFIABLE")
        outcome = report["investigation"]["outcome"]
        self.assertEqual(outcome["state_before"], "AFFINITY_ONLY")
        self.assertEqual(outcome["direction"], "more_cautious")
        self.assertTrue(outcome["changed"])
        self.assertIn("was lowered", report["investigation"]["decision"])
        # Lowering a verdict is still an audit result: its own receipts hold.
        self.assertTrue(candidate["receipts_verified"])
        self.assertEqual(candidate["investigation"]["direction"], "more_cautious")

    def test_a_re_read_cannot_make_an_unverifiable_verdict_supported(self):
        adapter = ScriptedAdapter(
            candidates=[(SPARSE_LIST, 0.70)], direct={"t-sparse": SPARSE_LIST}
        )
        report = run(adapter)
        candidate = report["candidates"][0]
        self.assertEqual(candidate["state"], "UNVERIFIABLE")
        self.assertFalse(report["investigation"]["outcome"]["changed"])
        # Fewer than two dimensions are evaluable, which the reason must say.
        self.assertIn("at least 2", candidate["reason"])

    def test_the_re_read_never_supplies_an_affinity(self):
        """Direct evidence and the affinity signal stay separate in the record."""
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80)], direct={"t-thin": THIN_DIRECT}
        )
        report = run(adapter)
        candidate = report["candidates"][0]
        self.assertEqual(candidate["affinity"], 0.80)
        joined = " ".join(candidate["limitations"])
        self.assertIn("affinity value is unchanged", joined)
        self.assertIn("never a score", joined)

    def test_the_agent_only_ever_adds_terms_that_the_source_actually_has(self):
        """A forged re-read cannot manufacture support, even one that claims CORROBORATED."""
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80)], direct={"t-thin": THIN_DIRECT}
        )

        def forge(seed_payload, candidate_payload, affinity, **kwargs):
            result = audit_pair(seed_payload, candidate_payload, affinity, **kwargs)
            if candidate_payload is not THIN_DIRECT:
                return result
            return replace(
                result,
                state="CORROBORATED",
                receipts_verified=False,
                verification_problems=["themes: reported shared terms do not match payload"],
            )

        with mock.patch("second_opinion.agent.audit_pair", side_effect=forge):
            report = run(adapter)
        candidate = report["candidates"][0]
        self.assertEqual(candidate["state"], "AFFINITY_ONLY")
        self.assertNotIn("investigation", candidate)
        self.assertFalse(report["investigation"]["outcome"]["changed"])
        self.assertIn("could not be re-derived", report["investigation"]["outcome"]["detail"])
        failed = [s for s in report["trace"] if s["step"] == "verify_receipts" and s["status"] == "failed"]
        self.assertTrue(failed)
        self.assertTrue(any(s["status"] == "failed" for s in report["trace"]))


class TestFailurePaths(unittest.TestCase):
    """Every way the follow-up can fail still ends in a valid, honest report."""

    def pristine(self, candidates):
        """The first-pass report the reader would have got with no follow-up."""
        adapter = ScriptedAdapter(candidates=candidates)
        return run(adapter, max_follow_up_queries=0)["candidates"][0]

    def assert_first_pass_stand(self, report, before):
        candidate = report["candidates"][0]
        self.assertEqual(candidate["state"], before["state"])
        self.assertEqual(candidate["dimensions"], before["dimensions"])
        self.assertEqual(candidate["affinity"], before["affinity"])
        self.assertNotIn("investigation", candidate)
        self.assertEqual(report["investigation"]["outcome"]["changed"], False)
        steps = [s["step"] for s in report["trace"]]
        self.assertIn("follow_up_query", steps)
        self.assertIn("investigation_outcome", steps)
        self.assertEqual(steps[-1], "decide")
        for entry in report["trace"]:
            self.assertIn(entry["status"], {"ok", "failed", "stopped"})

    def test_timeout_keeps_the_first_pass_verdict(self):
        before = self.pristine([(THIN_LIST, 0.80)])
        adapter = ScriptedAdapter(candidates=[(THIN_LIST, 0.80)],
                                  fail=UpstreamTimeout("Qloo timed out"))
        report = run(adapter)
        self.assert_first_pass_stand(report, before)
        self.assertIn("timed out", report["investigation"]["outcome"]["detail"])
        self.assertEqual(report["investigation"]["outcome"]["receipts_verified"], None)

    def test_authentication_failure_is_named_as_such(self):
        before = self.pristine([(THIN_LIST, 0.80)])
        adapter = ScriptedAdapter(candidates=[(THIN_LIST, 0.80)],
                                  fail=AuthenticationError("HTTP 401 from Qloo"))
        report = run(adapter)
        self.assert_first_pass_stand(report, before)
        self.assertIn("credential was rejected", report["investigation"]["outcome"]["detail"])

    def test_a_generic_adapter_error_is_reported_without_a_crash(self):
        before = self.pristine([(THIN_LIST, 0.80)])
        adapter = ScriptedAdapter(candidates=[(THIN_LIST, 0.80)],
                                  fail=AdapterError("upstream refused the lookup"))
        report = run(adapter)
        self.assert_first_pass_stand(report, before)
        self.assertIn("upstream refused the lookup", report["investigation"]["outcome"]["detail"])

    def test_no_direct_record_leaves_the_gap_unresolved(self):
        before = self.pristine([(THIN_LIST, 0.80)])
        adapter = ScriptedAdapter(candidates=[(THIN_LIST, 0.80)], missing=True)
        report = run(adapter)
        self.assert_first_pass_stand(report, before)
        self.assertIn("no record of its own", report["investigation"]["outcome"]["detail"])
        self.assertIn("could not be resolved either way", report["investigation"]["decision"])

    def test_an_unreadable_record_is_abandoned(self):
        before = self.pristine([(THIN_LIST, 0.80)])
        adapter = ScriptedAdapter(candidates=[(THIN_LIST, 0.80)], corrupt=True)
        report = run(adapter)
        self.assert_first_pass_stand(report, before)
        self.assertIn("could not be parsed", report["investigation"]["outcome"]["detail"])

    def test_a_refused_audit_carries_no_investigation(self):
        """Steps 6-10 are never reached when the first pass refuses."""
        adapter = ScriptedAdapter(candidates=[(THIN_LIST, 0.80)])
        with mock.patch.object(adapter, "resolve_seed",
                               side_effect=EntityResolutionError("no such entity")):
            report = run(adapter)
        self.assertFalse(report["ok"])
        self.assertIsNone(report["investigation"])
        self.assertEqual(report["trace"][-1]["step"], "refuse")


class TestReceiptsAndProvenance(unittest.TestCase):
    """The second pass is evidence only if its receipts still resolve."""

    def test_provenance_points_at_the_record_the_follow_up_fetched(self):
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80)], direct={"t-thin": THIN_DIRECT}
        )
        report = run(adapter)
        dims = report["candidates"][0]["dimensions"]
        audited_against = []
        for name, receipt in dims.items():
            for path in receipt["seed_sources"]:
                resolve_field(SEED, path)
            for path in receipt["candidate_sources"]:
                resolve_field(THIN_DIRECT, path)
                audited_against.append((name, path))
        self.assertGreater(len(audited_against), 0)
        # The audience term exists only in the re-read record: the list record had
        # no tags at all, so a receipt naming it could not have come from there.
        audience_paths = dims["audience"]["candidate_sources"]
        self.assertTrue(audience_paths)
        for path in audience_paths:
            with self.assertRaises(KeyError):
                resolve_field(THIN_LIST, path)

    def test_every_reported_candidate_keeps_verified_receipts(self):
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80), (RICH_LIST, 0.90), (SPARSE_LIST, 0.70)],
            direct={"t-thin": THIN_DIRECT, "t-sparse": SPARSE_LIST},
        )
        report = run(adapter)
        for candidate in report["candidates"]:
            self.assertTrue(candidate["receipts_verified"], candidate["candidate"])
            self.assertEqual(candidate["verification_problems"], [])

    def test_the_trace_records_the_gap_the_reason_the_action_and_the_result(self):
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80), (SPARSE_LIST, 0.70)],
            direct={"t-thin": THIN_DIRECT, "t-sparse": SPARSE_LIST},
        )
        report = run(adapter)
        steps = [(s["step"], s["status"]) for s in report["trace"]]
        order = [name for name, _ in steps]
        self.assertEqual(order[:3], ["resolve_seed", "retrieve_candidates", "extract_metadata"])
        self.assertEqual(order[-1], "decide")
        for required in ("identify_gap", "plan_follow_up", "follow_up_query",
                         "investigation_outcome"):
            self.assertIn(required, order)
        self.assertLess(order.index("identify_gap"), order.index("plan_follow_up"))
        self.assertLess(order.index("plan_follow_up"), order.index("follow_up_query"))
        self.assertLess(order.index("follow_up_query"), order.index("investigation_outcome"))
        # One receipt check per candidate in the first pass (2), plus the follow-up's.
        self.assertEqual(steps.count(("verify_receipts", "ok")), 3)
        decide = next(s for s in report["trace"] if s["step"] == "decide")
        self.assertIn("1 follow-up lookup(s) of a permitted 1", decide["detail"])


class TestNothingLeaks(unittest.TestCase):
    """The investigation text is operator-facing prose, not a dump."""

    def serialized(self, report):
        return json.dumps(report, sort_keys=True)

    def test_no_credential_or_path_appears_in_an_investigated_report(self):
        for scenario in ("improves", "confirms"):
            report = run(SyntheticAdapter(scenario), DEMO_SEED)
            text = self.serialized(report)
            self.assertNotIn(SENTINEL, text)
            self.assertNotIn("Traceback", text)
            for needle in ("/Users/", "/var/folders/", "/private/", "second_opinion/agent.py"):
                self.assertNotIn(needle, text, scenario)

    def test_auth_and_timeout_are_described_by_the_agent_not_the_transport(self):
        """The canned wording replaces the raw message, so a key or a local path
        carried inside an exception string cannot ride into a report."""
        for failure in (
            AuthenticationError(f"HTTP 401 for key {SENTINEL}"),
            UpstreamTimeout("timed out reading /Users/someone/.env"),
        ):
            adapter = ScriptedAdapter(candidates=[(THIN_LIST, 0.80)], fail=failure)
            text = self.serialized(run(adapter))
            self.assertNotIn(SENTINEL, text)
            self.assertNotIn("/Users/", text)
            self.assertNotIn("Traceback", text)

    def test_an_adapter_message_passes_through_unchanged(self):
        """What the agent adds around a generic failure is nothing at all.

        The adapter layer owns the text of its own errors; the investigation pass
        must not decorate it with paths, stack frames or credentials of its own.
        """
        adapter = ScriptedAdapter(
            candidates=[(THIN_LIST, 0.80)], fail=AdapterError("upstream refused the lookup")
        )
        report = run(adapter)
        self.assertEqual(report["investigation"]["outcome"]["detail"], "upstream refused the lookup")
        text = self.serialized(report)
        self.assertNotIn("Traceback", text)
        self.assertNotIn(__file__, text)

    def test_the_investigation_block_is_always_fully_populated(self):
        report = run(SyntheticAdapter("improves"), DEMO_SEED)
        investigation = report["investigation"]
        for key in ("max_follow_up_queries", "follow_up_queries_used", "triggered",
                    "gap", "reason", "action", "outcome", "candidates_with_gap",
                    "decision"):
            self.assertIn(key, investigation)
        self.assertIsInstance(investigation["decision"], str)
        self.assertTrue(investigation["decision"])


class TestShippedScenarios(unittest.TestCase):
    """The two demonstrations the sidebar offers must show what it claims.

    `improves` is advertised as a gap a direct re-read closes, `confirms` as one
    it does not. The README and the UI repeat that, so it is pinned here against
    the real agent and the real rules.
    """

    def reports(self):
        return {
            scenario: run(SyntheticAdapter(scenario), DEMO_SEED) for scenario in ("improves", "confirms")
        }

    def test_improves_closes_the_gap_and_moves_the_verdict(self):
        report = self.reports()["improves"]
        investigation = report["investigation"]
        self.assertEqual(investigation["gap"]["candidate"], "Cassette Harbour")
        self.assertEqual(investigation["candidates_with_gap"], 2)
        self.assertEqual(investigation["follow_up_queries_used"], 1)
        hit = next(c for c in report["candidates"] if c.get("investigation"))
        self.assertEqual(hit["candidate"], "Cassette Harbour")
        self.assertEqual(hit["investigation"]["state_before"], "AFFINITY_ONLY")
        self.assertEqual(hit["state"], "CORROBORATED")
        self.assertTrue(hit["receipts_verified"])

        # Independent check of the arithmetic behind the improvement:
        #   genres   {jangle pop} vs {indie folk, folk, chamber pop}      0/1 = 0.000
        #   themes   {longing} vs {longing, night travel, small towns}     1/1 = 1.000
        #   audience {folk traditionalists, arena pop listeners} vs seed   1/2 = 0.500
        #   style    {intimate} vs {intimate, warm}                        1/1 = 1.000
        dims = hit["dimensions"]
        self.assertAlmostEqual(dims["genres"]["overlap"], 0.0)
        self.assertAlmostEqual(dims["themes"]["overlap"], 1.0)
        self.assertAlmostEqual(dims["audience"]["overlap"], 0.5)
        self.assertAlmostEqual(dims["style"]["overlap"], 1.0)

    def test_confirms_holds_the_verdict_when_the_direct_record_is_just_as_thin(self):
        report = self.reports()["confirms"]
        investigation = report["investigation"]
        self.assertEqual(investigation["gap"]["candidate"], "Velvet Ferry")
        hit = next(c for c in report["candidates"] if c.get("investigation"))
        self.assertEqual(hit["candidate"], "Velvet Ferry")
        self.assertEqual(hit["state"], "AFFINITY_ONLY")
        self.assertEqual(hit["investigation"]["state_before"], "AFFINITY_ONLY")
        self.assertEqual(hit["investigation"]["newly_comparable"], ["audience"])
        self.assertIn("conservative direction", investigation["decision"])

    def test_only_the_chosen_candidate_is_re_read_in_either_scenario(self):
        for scenario, expected in (("improves", "demo-entity-0002"),
                                   ("confirms", "demo-entity-0003")):
            adapter = SyntheticAdapter(scenario)
            report = run(adapter, DEMO_SEED)
            lookups = adapter.queries.count("entity_record")
            # one for the seed record, one for the chosen candidate.
            self.assertEqual(lookups, 2, scenario)
            self.assertEqual(len(report["investigation"]["gap"]["candidate_id"]), len(expected))
            self.assertEqual(report["investigation"]["gap"]["candidate_id"], expected)
            investigated = [c["candidate"] for c in report["candidates"] if c.get("investigation")]
            self.assertEqual(len(investigated), 1, scenario)

    def test_the_demo_records_are_not_derived_from_the_captures(self):
        """A guard against a future edit swapping real data into the demo."""
        for record in list(LIST_RECORDS.values()) + list(DIRECT_RECORDS.values()) + [SEED_RECORD]:
            self.assertTrue(record["entity_id"].startswith("demo-entity-"), record["entity_id"])
        for scenario_map in AFFINITIES.values():
            self.assertTrue(all(value is None or 0.0 <= value <= 1.0 for value in scenario_map.values()))
        self.assertEqual(len(ORDER), len(LIST_RECORDS))
        self.assertEqual(DEMO_SEED, "Northbound Echo")

    def test_the_agent_docstring_step_list_matches_the_trace_it_emits(self):
        report = run(SyntheticAdapter("improves"), DEMO_SEED)
        order = [s["step"] for s in report["trace"]]
        for step in ("identify_gap", "plan_follow_up", "follow_up_query",
                     "investigation_outcome", "decide"):
            self.assertIn(step, order)
        self.assertIn("identify_gap", agent_module.__doc__)
        self.assertIn("follow_up_query", agent_module.__doc__)


class TestDemoEndpoint(unittest.TestCase):
    """`/api/demo`: the same pass, served with no key, no data and no budget.

    A judge's laptop has no credential and a clean checkout has no captures, so
    the demonstration has to be reachable over HTTP. These tests pin that it is
    reachable, that it labels itself synthetic, and that it costs nothing the
    audit budgets are there to protect.
    """

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
        # A credential present in the environment must change nothing about the
        # demonstration, and must never reach its response.
        patcher = mock.patch.dict(os.environ, {"QLOO_API_KEY": SENTINEL})
        patcher.start()
        self.addCleanup(patcher.stop)
        live = AuditThrottle(per_minute=6, burst=3, concurrent=2, mode="live")
        fixture = AuditThrottle(per_minute=30, burst=10, concurrent=4, mode="fixture")
        set_default_throttle(live, "live")
        set_default_throttle(fixture, "fixture")
        self.addCleanup(set_default_throttle, None)
        self.live, self.fixture = live, fixture

    def get(self, path):
        status, headers, body = _request(self.port, path, "198.51.100.7")
        return status, headers, json.loads(body)

    def test_the_demo_runs_with_no_live_client_at_all(self):
        """Structural proof: the demo path cannot build a Qloo client."""

        def explode(*args, **kwargs):
            raise AssertionError("the demonstration reached for the live Qloo adapter")

        with mock.patch("second_opinion.server.LiveAdapter", explode):
            status, _, payload = self.get("/api/demo?scenario=improves")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_the_demo_reports_synthetic_mode_and_the_investigation(self):
        status, _, payload = self.get("/api/demo?scenario=improves")
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "synthetic")
        self.assertEqual(payload["demo"]["scenario"], "improves")
        self.assertIn("invented", payload["demo"]["evidence_origin"])
        self.assertEqual(payload["demo"]["seed_queries"], 1)
        self.assertEqual(payload["demo"]["entity_lookups"], 2)
        self.assertNotIn(SENTINEL, json.dumps(payload, sort_keys=True))
        self.assertIsNotNone(payload["investigation"]["gap"])
        steps = [s["step"] for s in payload["trace"]]
        self.assertIn("follow_up_query", steps)

    def test_the_two_scenarios_choose_different_gaps(self):
        """The sidebar's two demos must not be the same run twice."""
        chosen = {}
        for scenario in ("improves", "confirms"):
            _, _, payload = self.get(f"/api/demo?scenario={scenario}")
            self.assertEqual(payload["mode"], "synthetic", scenario)
            self.assertEqual(payload["demo"]["entity_lookups"], 2, scenario)
            chosen[scenario] = payload["investigation"]["gap"]["candidate"]
        self.assertEqual(chosen, {"improves": "Cassette Harbour", "confirms": "Velvet Ferry"})

    def test_an_unknown_scenario_is_refused_without_running_the_agent(self):
        status, _, payload = self.get("/api/demo?scenario=totally-made-up")
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("improves or confirms", payload["error"])

    def test_a_demo_never_appears_in_an_audit_budget(self):
        """Twenty demonstrations must not cost one request of the live quota."""
        for index in range(20):
            status, _, _ = self.get(f"/api/demo?scenario={'improves' if index % 2 else 'confirms'}")
            self.assertEqual(status, 200)
        self.assertEqual(self.live.limiter.tracked_keys(), 0)
        self.assertEqual(self.fixture.limiter.tracked_keys(), 0)
        self.assertTrue(self.live.gate.try_acquire())

    def test_a_crash_in_the_demo_path_sends_no_traceback(self):
        logs = io.StringIO()
        with mock.patch(
            "second_opinion.server.SecondOpinionAgent",
            side_effect=RuntimeError(f"demo boom at /Users/operator/{SENTINEL}"),
        ), mock.patch("sys.stderr", logs):
            status, _, payload = self.get("/api/demo?scenario=improves")
        text = json.dumps(payload)
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "internal demo error")
        self.assertNotIn("Traceback", text)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("/Users/", text)
        # The operator keeps the frames; the credential does not ride along.
        # `_redact_credential` matches the key's literal bytes, and the sentinel
        # above *is* the key in this test's environment.
        self.assertIn("RuntimeError", logs.getvalue())
        self.assertIn("/Users/operator/", logs.getvalue())
        self.assertNotIn(SENTINEL, logs.getvalue())
        self.assertIn("[credential redacted]", logs.getvalue())


if __name__ == "__main__":
    unittest.main()
