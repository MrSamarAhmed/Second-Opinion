"""The agent: a small, auditable state machine.

Steps, executed in order and recorded in a trace that the UI renders verbatim:

  1. resolve_seed          map the user's query to an entity id
  2. retrieve_candidates   fetch the recommendation set for that seed
  3. extract_metadata      build dimension profiles from the payloads
  4. evaluate_dimensions   compare the four dimensions
  5. verify_receipts       re-derive every receipt from the raw payload
  6. decide                emit a state, or refuse to make a claim

If any step cannot be completed the agent stops and returns a refusal. It never
fills a gap with a plausible value.
"""

from __future__ import annotations

from typing import List, Optional

from .adapters import AdapterError, AuthenticationError, EntityResolutionError, UpstreamTimeout
from .audit import audit_pair, rules_payload
from .config import DEFAULT_CONFIG, SCORED_DIMENSIONS, AuditConfig
from .evidence import extract_profile


def _failure_code(exc: AdapterError, default: str) -> str:
    """Name the failure precisely; an auth or timeout error is not a lookup miss."""
    if isinstance(exc, AuthenticationError):
        return "authentication failed"
    if isinstance(exc, UpstreamTimeout):
        return "upstream timeout"
    return default


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
    def __init__(self, adapter, config: AuditConfig = DEFAULT_CONFIG):
        self.adapter = adapter
        self.config = config

    def audit(self, seed_query: str, limit: Optional[int] = None) -> dict:
        trace = Trace()

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

        trace.add(
            "decide", "ok",
            f"emitted {len(audited)} audit result(s) under the frozen thresholds",
        )

        return {
            "ok": True,
            "refusal": None,
            "seed": summary,
            "seed_label": ref.label,
            "candidates": audited,
            "dimension_summary": self._dimension_summary(audited),
            "trace": trace.steps,
            "rules": rules_payload(self.config),
            "mode": getattr(self.adapter, "mode", "unknown"),
        }

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
            "trace": trace.steps,
            "rules": rules_payload(self.config),
            "mode": getattr(self.adapter, "mode", "unknown"),
        }
