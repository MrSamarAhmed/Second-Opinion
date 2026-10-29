"""The agent: a small, auditable state machine.

Steps, executed in order and recorded in a trace that the UI renders verbatim:

  1. resolve_seed          map the user's query to an entity id
  2. retrieve_candidates   fetch the recommendation set for that seed
  3. extract_metadata      build dimension profiles from the payloads
  4. evaluate_dimensions   compare the four dimensions
  5. verify_receipts       re-derive every receipt from the raw payload
  6. identify_gap          ask whether any verdict rests on a one-sided gap
  7. plan_follow_up        decide if one further lookup could reduce that doubt
  8. follow_up_query       at most ONE targeted re-read of the entity's own record
  9. verify_receipts       re-derive the new receipts under the same rules
  10. investigation_outcome commit the revised verdict, or decline to revise it
  11. decide               emit a state, or refuse to make a claim

If any step cannot be completed the agent stops and returns a refusal. It never
fills a gap with a plausible value.

Steps 6 to 10 are the whole of the agent's discretion, and it is bounded: one
follow-up lookup per audit, enforced by a counter that a failed query still
consumes. The follow-up never invents support — it re-reads the same evidence
store for the entity whose verdict looked thinnest, re-runs the identical audit
and receipt rules over whatever comes back, and keeps the first-pass verdict if
the new evidence does not hold up or does not arrive at all. The verdict it
returns may therefore improve, stay unchanged, or move in the cautious
direction, and the trace says which of the three happened and why.
"""

from __future__ import annotations

from typing import List, Optional

from .adapters import AdapterError, AuthenticationError, EntityResolutionError, UpstreamTimeout
from .audit import audit_pair, rules_payload
from .config import DEFAULT_CONFIG, SCORED_DIMENSIONS, AuditConfig
from .evidence import extract_profile

# The ceiling on targeted follow-up lookups per audit. A caller may lower it to
# zero (never investigate) but cannot raise it: this is the number that bounds
# how much extra Qloo quota one audit is allowed to spend.
MAX_FOLLOW_UP_QUERIES = 1

# The three verdicts ordered by how much they claim, used only to describe which
# way a re-read moved a verdict. A follow-up may raise a claim, hold it, or lower
# it; the third case is the one that matters, because the entity's own record is
# better evidence than the copy that arrived inside the recommendation list.
_STATE_RANK = {"UNVERIFIABLE": 0, "AFFINITY_ONLY": 1, "CORROBORATED": 2}


def _direction(before: str, after: str) -> str:
    if _STATE_RANK.get(after, 0) > _STATE_RANK.get(before, 0):
        return "improved"
    if _STATE_RANK.get(after, 0) < _STATE_RANK.get(before, 0):
        return "more_cautious"
    return "held"


def _failure_code(exc: AdapterError, default: str) -> str:
    """Name the failure precisely; an auth or timeout error is not a lookup miss."""
    if isinstance(exc, AuthenticationError):
        return "authentication failed"
    if isinstance(exc, UpstreamTimeout):
        return "upstream timeout"
    return default


def _lookup_gap_reason(exc: AdapterError) -> str:
    """A follow-up failure said in the terms the reader needs to act on."""
    code = _failure_code(exc, "follow-up lookup failed")
    return {
        "authentication failed": "the credential was rejected, so no further evidence is reachable",
        "upstream timeout": "the follow-up request to Qloo timed out",
    }.get(code, str(exc))


def _one_sided_gaps(receipts: dict) -> List[str]:
    """Dimensions this audit could not compare because the record on hand is empty there.

    `receipts` is one serialized candidate result's dimension mapping. A gap is
    *one-sided* when the seed has terms and the record used for the candidate has
    none: that is the difference between "these two artists share nothing here"
    and "the record I was given says nothing here", and only the second one is an
    artifact of how the entity was fetched.
    """
    return [
        name
        for name, receipt in receipts.items()
        if receipt.get("status") == "unverifiable"
        and receipt.get("seed_terms")
        and not receipt.get("candidate_terms")
    ]


class Trace:
    def __init__(self) -> None:
        self.steps: List[dict] = []

    def add(self, step: str, status: str, detail: str, **extra) -> None:
        entry = {"step": step, "status": status, "detail": detail}
        entry.update(extra)
        self.steps.append(entry)


def _seed_summary(profile) -> dict:
    return {
        "entity_id": profile.entity_id,
        "name": profile.name,
        "usable": profile.is_usable(),
        "tag_count": profile.tag_count,
        "property_count": profile.property_count,
        "missing_dimensions": [
            d for d in SCORED_DIMENSIONS if not profile.dimensions.get(d)
        ],
        "dimension_sizes": {
            d: len(profile.dimensions.get(d) or []) for d in SCORED_DIMENSIONS
        },
    }


class SecondOpinionAgent:
    def __init__(
        self,
        adapter,
        config: AuditConfig = DEFAULT_CONFIG,
        max_follow_up_queries: int = MAX_FOLLOW_UP_QUERIES,
    ):
        self.adapter = adapter
        self.config = config
        # Clamped, not trusted: a caller can switch the investigation pass off but
        # cannot raise the ceiling, because this number bounds how much extra Qloo
        # quota a single audit is allowed to spend.
        self.max_follow_up_queries = max(
            0, min(int(max_follow_up_queries), MAX_FOLLOW_UP_QUERIES)
        )
        self._follow_ups_used = 0

    def audit(self, seed_query: str, limit: Optional[int] = None) -> dict:
        trace = Trace()
        # The follow-up budget belongs to one audit, not to the agent object.
        self._follow_ups_used = 0

        # 1. resolve seed
        try:
            ref = self.adapter.resolve_seed(seed_query)
            trace.add("resolve_seed", "ok", f"resolved {seed_query!r} to {ref.name} ({ref.entity_id})")
        except EntityResolutionError as exc:
            trace.add("resolve_seed", "failed", str(exc))
            return self._refusal(trace, None, "seed resolution failed", str(exc))
        except AdapterError as exc:
            trace.add("resolve_seed", "failed", str(exc))
            return self._refusal(trace, None, _failure_code(exc, "seed resolution failed"), str(exc))

        # 2. retrieve candidates
        try:
            candidates = self.adapter.candidates(ref.entity_id)
            trace.add(
                "retrieve_candidates", "ok",
                f"retrieved {len(candidates)} candidate entities",
                count=len(candidates),
                affinities_present=sum(1 for _, a in candidates if a is not None),
            )
        except EntityResolutionError as exc:
            trace.add("retrieve_candidates", "failed", str(exc))
            return self._refusal(trace, ref, "candidate retrieval failed", str(exc))
        except AdapterError as exc:
            trace.add("retrieve_candidates", "failed", str(exc))
            return self._refusal(trace, ref, _failure_code(exc, "candidate retrieval failed"), str(exc))

        # 3. extract seed metadata
        try:
            seed_payload = self.adapter.seed_payload(ref.entity_id)
        except EntityResolutionError as exc:
            trace.add("extract_metadata", "failed", str(exc))
            return self._refusal(trace, ref, "seed metadata missing", str(exc))
        except AdapterError as exc:
            trace.add("extract_metadata", "failed", str(exc))
            return self._refusal(
                trace, ref, _failure_code(exc, "seed metadata retrieval failed"), str(exc)
            )
        if seed_payload is None:
            trace.add("extract_metadata", "failed", "no payload exists for the seed")
            return self._refusal(trace, ref, "seed metadata missing", "the seed has no stored payload")
        try:
            seed_profile = extract_profile(seed_payload)
        except ValueError as exc:
            trace.add("extract_metadata", "failed", str(exc))
            return self._refusal(trace, ref, "seed metadata malformed", str(exc))

        summary = _seed_summary(seed_profile)
        if not seed_profile.is_usable():
            trace.add(
                "extract_metadata", "failed",
                f"seed {seed_profile.name} has {seed_profile.tag_count} tags and no usable "
                f"terms in any scored dimension",
                seed=summary,
            )
            detail = (
                f"The seed entity {seed_profile.name} returned {seed_profile.tag_count} tags and "
                f"no genres, themes, audience or style terms. With no seed-side evidence, every "
                f"dimension comparison would be one-sided, so no audit is possible."
            )
            afford = {
                "candidate_count": len(candidates),
                "affinities_present": sum(1 for _, a in candidates if a is not None),
                "seed_tag_count": seed_profile.tag_count,
                "missing_dimensions": summary["missing_dimensions"],
            }
            return self._refusal(trace, ref, "insufficient seed metadata", detail, extra=afford)
        trace.add(
            "extract_metadata", "ok",
            f"seed carries terms in {len(SCORED_DIMENSIONS) - len(summary['missing_dimensions'])} "
            f"of {len(SCORED_DIMENSIONS)} scored dimensions",
            seed=summary,
        )

        # 4-6. per candidate
        audited: List[dict] = []
        for entity, affinity in (candidates[:limit] if limit else candidates):
            name = entity.get("name", entity.get("entity_id"))
            try:
                profile = extract_profile(entity)
            except ValueError as exc:
                trace.add("extract_metadata", "failed", f"{name}: {exc}")
                audited.append({
                    "candidate": name,
                    "state": "UNVERIFIABLE",
                    "reason": f"candidate payload could not be read: {exc}",
                })
                continue

            result = audit_pair(
                seed_payload, entity, affinity, config=self.config, verify=True
            )
            if not result.receipts_verified:
                trace.add(
                    "verify_receipts", "failed",
                    f"{name}: {'; '.join(result.verification_problems)}",
                )
                payload = result.to_dict()
                payload["state"] = "UNVERIFIABLE"
                payload["reason"] = (
                    "Receipt verification failed, so the audit is void: "
                    + "; ".join(result.verification_problems)
                )
                audited.append(payload)
                continue

            trace.add(
                "verify_receipts", "ok",
                f"{name}: {sum(1 for r in result.dimensions.values() if r.status != 'unverifiable')} "
                f"dimension receipts re-derived from the payload",
            )
            audited.append(result.to_dict())

        investigation = self._investigate(seed_payload, audited, trace)

        trace.add(
            "decide", "ok",
            f"emitted {len(audited)} audit result(s) under the frozen thresholds, after "
            f"{investigation['follow_up_queries_used']} follow-up lookup(s) of a permitted "
            f"{investigation['max_follow_up_queries']}",
        )

        return {
            "ok": True,
            "refusal": None,
            "seed": summary,
            "seed_label": ref.label,
            "candidates": audited,
            "dimension_summary": self._dimension_summary(audited),
            "investigation": investigation,
            "trace": trace.steps,
            "rules": rules_payload(self.config),
            "mode": getattr(self.adapter, "mode", "unknown"),
        }

    # -- the bounded investigation pass ----------------------------------
    def _eligible_gaps(self, audited: List[dict]) -> List[dict]:
        """Candidates whose verdict may rest on a one-sided gap rather than on data.

        Deliberately narrow. A void audit (receipts that did not re-derive) is
        already the strongest possible negative and a re-read is not the remedy; a
        candidate with no affinity has no recommendation to audit, and no entity
        record can supply a value Qloo did not return. What is left is the case
        where the comparison was blocked because the record in hand said nothing
        in a dimension the seed does speak in.
        """
        eligible = []
        for entry in audited:
            if not entry.get("receipts_verified"):
                continue
            if entry.get("state") not in ("AFFINITY_ONLY", "UNVERIFIABLE"):
                continue
            if entry.get("affinity") is None or not entry.get("candidate_id"):
                continue
            gaps = _one_sided_gaps(entry.get("dimensions") or {})
            if gaps:
                eligible.append({
                    "candidate": entry.get("candidate"),
                    "candidate_id": entry["candidate_id"],
                    "affinity": entry["affinity"],
                    "state": entry["state"],
                    "dimensions": gaps,
                })
        return eligible

    def _investigate(self, seed_payload: dict, audited: List[dict], trace) -> dict:
        """At most one targeted re-read, decided by an evidence gap, judged by the same rules.

        Returns a record of the whole decision — gap, reason, action, outcome,
        final decision — which the trace mirrors step by step and the UI renders.
        Every early return is a valid honest end state: the first-pass verdict
        stands and is reported as unimproved, never softened.
        """
        summary = {
            "max_follow_up_queries": self.max_follow_up_queries,
            "follow_up_queries_used": 0,
            "triggered": False,
            "gap": None,
            "reason": None,
            "action": None,
            "outcome": None,
            "candidates_with_gap": 0,
            "decision": "no evidence gap: every verdict rests on populated dimensions on both sides",
        }

        eligible = self._eligible_gaps(audited)
        summary["candidates_with_gap"] = len(eligible)
        if not eligible:
            trace.add(
                "identify_gap", "ok",
                "no follow-up warranted: no candidate's verdict is blocked by a dimension its "
                "own record leaves empty while the seed fills it",
            )
            return summary

        # One query of budget, so pick the gap worth spending it on: the highest
        # affinity among the candidates that share the same kind of doubt. Affinity
        # is Qloo's own ranking of "will this be acted on", not a similarity claim.
        gap = max(eligible, key=lambda item: item["affinity"])
        summary["gap"] = gap
        trace.add(
            "identify_gap", "ok",
            f"{gap['candidate']}: {', '.join(gap['dimensions'])} could not be compared because the "
            f"record used for that candidate carries no terms there while the seed does; first-pass "
            f"verdict {gap['state']} at affinity {gap['affinity']:.3f}",
            candidate=gap["candidate"],
        )

        reason = (
            "a by-id re-read of this entity's own record is the only available request that can "
            "distinguish 'these two share nothing in this dimension' from 'the record I was handed "
            "says nothing in this dimension'"
        )
        summary["reason"] = reason
        if self._follow_ups_used >= self.max_follow_up_queries:
            trace.add(
                "plan_follow_up", "stopped",
                f"no follow-up query is left in this audit's budget of "
                f"{self.max_follow_up_queries}, so {gap['candidate']} keeps its first-pass verdict",
            )
            summary["decision"] = "gap identified, but the follow-up budget was already spent: verdict unchanged"
            return summary

        others = len(eligible) - 1
        trace.add(
            "plan_follow_up", "ok",
            reason + f"; chosen over {others} other candidate(s) with the same kind of gap because it "
            f"carries the highest affinity, so it is the recommendation a listener is most likely to "
            f"act on. Budget for this audit: {self.max_follow_up_queries} follow-up lookup.",
        )

        summary["action"] = {
            "candidate": gap["candidate"],
            "candidate_id": gap["candidate_id"],
            "lookup": "the entity's own record, requested by entity id — the same adapter call the "
                      "seed record is read with (in live mode the documented "
                      "filter.results.entities lookup; no new endpoint, header or parameter)",
            "dimensions_under_doubt": gap["dimensions"],
        }
        # Spend the budget before issuing the query: a lookup that times out, is
        # refused or returns nothing has still cost the audit its one follow-up.
        self._follow_ups_used += 1
        summary["follow_up_queries_used"] = self._follow_ups_used
        summary["triggered"] = True

        try:
            record = self.adapter.seed_payload(gap["candidate_id"])
        except AdapterError as exc:
            return self._abandon(
                trace, summary, gap, _lookup_gap_reason(exc),
                "the follow-up lookup did not complete, so nothing new can be claimed about "
                f"{gap['candidate']}: its {gap['state']} verdict stands as reported",
            )

        if record is None:
            return self._abandon(
                trace, summary, gap,
                "the entity has no record of its own in this source to re-read",
                f"no direct record exists for {gap['candidate']}, so the gap could not be "
                "resolved either way and the first-pass verdict stands",
            )

        try:
            revised = audit_pair(
                seed_payload, record, gap["affinity"], config=self.config, verify=True
            )
        except ValueError as exc:
            return self._abandon(
                trace, summary, gap, f"the re-read record could not be parsed: {exc}",
                f"the record returned for {gap['candidate']} was unreadable, so the first-pass "
                "verdict stands",
            )

        trace.add(
            "follow_up_query", "ok",
            f"re-read {gap['candidate']}'s own record: {sum(1 for r in revised.dimensions.values() if r.status != 'unverifiable')} "
            f"of {len(SCORED_DIMENSIONS)} dimensions now comparable",
        )

        payload = revised.to_dict()
        if not revised.receipts_verified:
            trace.add(
                "verify_receipts", "failed",
                f"follow-up for {gap['candidate']}: {'; '.join(revised.verification_problems)}",
            )
            return self._abandon(
                trace, summary, gap,
                "the re-read's own receipts could not be re-derived from it, so it was discarded "
                "rather than trusted",
                f"{gap['candidate']} keeps its first-pass verdict: unverifiable evidence is not "
                "made true by arriving twice",
            )

        new_gaps = _one_sided_gaps(payload["dimensions"])
        trace.add(
            "verify_receipts", "ok",
            f"follow-up: {len(SCORED_DIMENSIONS) - len(new_gaps)} of {len(SCORED_DIMENSIONS)} "
            f"dimension receipts for {gap['candidate']} re-derived from the record just fetched",
        )

        before = gap["state"]
        direction = _direction(before, revised.state)
        newly_comparable = [
            name for name in gap["dimensions"]
            if (payload["dimensions"].get(name) or {}).get("status") != "unverifiable"
        ]
        payload["investigation"] = {
            "follow_up": True,
            "state_before": before,
            "state_after": revised.state,
            "changed": revised.state != before,
            "direction": direction,
            "dimensions_under_doubt": gap["dimensions"],
            "newly_comparable": newly_comparable,
            "record_source": "fetched by entity id in this audit, not lifted from the "
                             "recommendation list",
        }
        payload["limitations"] = list(revised.limitations) + [
            f"This candidate's record was re-read directly by entity id after the first pass found "
            f"one-sided gaps in {', '.join(gap['dimensions'])}. The affinity value is unchanged from "
            "the recommendation list: the re-read supplied metadata, never a score."
        ]
        index = next(
            i for i, entry in enumerate(audited) if entry.get("candidate_id") == gap["candidate_id"]
        )
        audited[index] = payload

        if revised.state == before:
            if newly_comparable:
                if new_gaps:
                    decision = (
                        f"verdict unchanged at {before}: the direct record made "
                        f"{', '.join(newly_comparable)} comparable, but {', '.join(new_gaps)} still "
                        "holds nothing on the candidate's side and the state rule is not met, so the "
                        "doubt is settled in the conservative direction"
                    )
                else:
                    decision = (
                        f"verdict unchanged at {before}: the direct record made "
                        f"{', '.join(newly_comparable)} comparable and left no one-sided gap behind, "
                        "but the state rule is still not met, so the doubt is settled in the "
                        "conservative direction"
                    )
            else:
                decision = (
                    f"verdict unchanged at {before}: the direct record is just as empty in "
                    f"{', '.join(gap['dimensions'])} as the list record was, so the gap is real "
                    "rather than a fetching artifact"
                )
        elif direction == "improved":
            decision = (
                f"{before} -> {revised.state}: the direct record populated "
                f"{', '.join(newly_comparable) or 'nothing further'} and those receipts re-derived"
            )
        else:
            decision = (
                f"{before} -> {revised.state}: the entity's own record carries less than the copy "
                f"that arrived inside the recommendation list, so the first pass was claiming more "
                "than the direct evidence supports and the verdict was lowered"
            )
        summary["outcome"] = {
            "state_before": before,
            "state_after": revised.state,
            "changed": revised.state != before,
            "direction": direction,
            "newly_comparable": newly_comparable,
            "remaining_gaps": new_gaps,
            "receipts_verified": True,
        }
        summary["decision"] = decision
        trace.add("investigation_outcome", "ok", decision)
        return summary

    def _abandon(self, trace, summary, gap, why, decision) -> dict:
        """End the investigation without a usable new verdict: the first pass stands."""
        trace.add("follow_up_query", "failed", why)
        summary["outcome"] = {
            "state_before": gap["state"],
            "state_after": gap["state"],
            "changed": False,
            "direction": "held",
            "newly_comparable": [],
            "receipts_verified": None,
            "detail": why,
        }
        summary["decision"] = decision
        trace.add("investigation_outcome", "stopped", decision)
        return summary

    def _dimension_summary(self, audited: List[dict]) -> dict:
        """Per-dimension status counts for this candidate set.

        `is_blanket` marks a dimension corroborated for every candidate. Such a
        dimension carries no discriminative power for this seed, so treating it
        as one of the "two independent dimensions" overstates the evidence. The
        UI shows this next to the verdicts rather than hiding it.
        """
        summary = {}
        total = len(audited)
        for dim in SCORED_DIMENSIONS:
            counts = {"corroborated": 0, "weak": 0, "unsupported": 0, "unverifiable": 0}
            for cand in audited:
                receipt = (cand.get("dimensions") or {}).get(dim)
                if receipt:
                    counts[receipt["status"]] = counts.get(receipt["status"], 0) + 1
            counts["is_blanket"] = total > 1 and counts["corroborated"] == total
            counts["total"] = total
            summary[dim] = counts
        return summary

    def _refusal(self, trace, ref, code, detail, extra=None) -> dict:
        trace.add("refuse", "stopped", detail)
        return {
            "ok": False,
            "refusal": {
                "code": code,
                "detail": detail,
                "seed": ref.name if ref else None,
                "seed_id": ref.entity_id if ref else None,
                "facts": extra or {},
            },
            "seed": None,
            "seed_label": ref.label if ref else None,
            "candidates": [],
            "investigation": None,
            "trace": trace.steps,
            "rules": rules_payload(self.config),
            "mode": getattr(self.adapter, "mode", "unknown"),
        }
