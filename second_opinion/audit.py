"""The audit: turn four dimension receipts into one discrete state.

There is intentionally no `confidence` field and no weighted total. The only
output that aggregates dimensions is a rule over their statuses, which keeps
every claim independently inspectable.
"""

from __future__ import annotations

from typing import List, Optional

from .config import AuditConfig, DEFAULT_CONFIG, SCORED_DIMENSIONS, state_definitions
from .evidence import compare_profiles, extract_profile, verify_receipts
from .schema import AuditResult

BASE_LIMITATION = (
    "Receipts describe overlap between Qloo's descriptive metadata terms. "
    "They are not evidence of shared fans, demand, attendance, ticket sales or revenue."
)


def decide_state(
    affinity: Optional[float],
    dimension_statuses: dict,
    seed_usable: bool,
    candidate_usable: bool,
    config: AuditConfig,
) -> tuple:
    """Return (state, reason). Pure function over statuses."""
    evaluable = [d for d, s in dimension_statuses.items() if s != "unverifiable"]
    corroborated = [d for d, s in dimension_statuses.items() if s == "corroborated"]

    if not seed_usable:
        return (
            "UNVERIFIABLE",
            "the seed entity has no usable metadata in any scored dimension, "
            "so no dimension can be compared.",
        )
    if not candidate_usable:
        return (
            "UNVERIFIABLE",
            "the candidate entity has no usable metadata in any scored dimension, "
            "so no dimension can be compared.",
        )
    if affinity is None:
        return (
            "UNVERIFIABLE",
            "Qloo returned no affinity value for this entity, so there is no "
            "recommendation to audit.",
        )
    if len(evaluable) < config.min_dimensions_evaluable:
        return (
            "UNVERIFIABLE",
            f"only {len(evaluable)} of {len(SCORED_DIMENSIONS)} dimensions carry "
            f"comparable data; at least {config.min_dimensions_evaluable} are required.",
        )
    if len(corroborated) >= config.min_dimensions_corroborated:
        return (
            "CORROBORATED",
            f"{len(corroborated)} of {len(SCORED_DIMENSIONS)} dimensions meet the "
            f"{config.strong_threshold:.2f} threshold: {', '.join(corroborated)}.",
        )
    return (
        "AFFINITY_ONLY",
        f"affinity {affinity:.3f} is present, but only {len(corroborated)} of "
        f"{len(SCORED_DIMENSIONS)} dimensions meet the {config.strong_threshold:.2f} "
        f"threshold; {len(evaluable)} dimension(s) were comparable.",
    )


def audit_pair(
    seed_payload: dict,
    candidate_payload: dict,
    affinity: Optional[float],
    config: AuditConfig = DEFAULT_CONFIG,
    verify: bool = True,
) -> AuditResult:
    seed = extract_profile(seed_payload)
    candidate = extract_profile(candidate_payload)
    dims = compare_profiles(seed, candidate, config)
    statuses = {d: r.status for d, r in dims.items()}
    state, reason = decide_state(
        affinity, statuses, seed.is_usable(), candidate.is_usable(), config
    )

    limitations: List[str] = [BASE_LIMITATION]
    unverifiable = [d for d, s in statuses.items() if s == "unverifiable"]
    if unverifiable:
        limitations.append(
            "No comparable data in: " + ", ".join(sorted(unverifiable)) + "."
        )
    collapsed = sum(r.duplicates_collapsed for r in dims.values())
    if collapsed:
        limitations.append(
            f"{collapsed} duplicate term(s) were collapsed before comparison."
        )
    if seed.tag_count == 0 or candidate.tag_count == 0:
        limitations.append("At least one entity returned zero tags from Qloo.")
    if affinity is None:
        limitations.append("Qloo returned no affinity value for this entity.")
    limitations.append(
        "`influenced_by` and `instruments` are shown for context but excluded from "
        "corroboration: proper names rarely intersect, and shared instruments such "
        "as \"Vocals\" are not discriminative."
    )

    problems: List[str] = []
    if verify:
        problems = verify_receipts(seed_payload, candidate_payload, _Shim(dims))

    result = AuditResult(
        seed=seed.name,
        seed_id=seed.entity_id,
        candidate=candidate.name,
        candidate_id=candidate.entity_id,
        affinity=affinity,
        state=state,
        reason=reason,
        dimensions=dims,
        display_only={
            name: [t.display for t in terms]
            for name, terms in candidate.display_only.items()
        },
        limitations=limitations,
        receipts_verified=not problems,
        verification_problems=problems,
        config={
            "strong_threshold": config.strong_threshold,
            "min_dimensions_evaluable": config.min_dimensions_evaluable,
            "min_dimensions_corroborated": config.min_dimensions_corroborated,
        },
    )
    return result


class _Shim:
    """verify_receipts only needs `.dimensions`."""

    def __init__(self, dimensions):
        self.dimensions = dimensions


def rules_payload(config: AuditConfig = DEFAULT_CONFIG) -> dict:
    return {
        "thresholds": {
            "strong_threshold": config.strong_threshold,
            "weak_threshold": config.weak_threshold,
            "min_dimensions_evaluable": config.min_dimensions_evaluable,
            "min_dimensions_corroborated": config.min_dimensions_corroborated,
        },
        "scored_dimensions": list(SCORED_DIMENSIONS),
        "display_only_dimensions": ["instruments", "influenced_by"],
        "states": state_definitions(config),
    }
