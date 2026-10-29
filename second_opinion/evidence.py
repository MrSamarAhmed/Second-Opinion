"""Extraction and comparison of the four evidence dimensions.

Field mapping, taken from the saved Qloo responses and nothing else:

  genres    properties.primary_genres[] + properties.secondary_genres[]
  themes    properties.lyrical_themes[]   (fallback: tags[type=urn:tag:theme:qloo].name)
  audience  tags[type=urn:tag:audience:qloo].name
  style     tags[type=urn:tag:style:qloo].name

Display-only, never scored:

  instruments    properties.instruments[]
  influenced_by  properties.influenced_by[]

Comparison is the overlap coefficient |A ∩ B| / min(|A|, |B|) over normalized,
de-duplicated term sets. The choice is deliberate: a raw Jaccard would punish an
artist merely for having a richer vocabulary, which is a property of the catalog
entry rather than of the relationship.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .config import SCORED_DIMENSIONS, AuditConfig
from .normalize import display_term, normalize_term
from .schema import DimensionResult, Profile, Term

THEME_TAG = "urn:tag:theme:qloo"
AUDIENCE_TAG = "urn:tag:audience:qloo"
STYLE_TAG = "urn:tag:style:qloo"


def _terms_from_list(values, field_name: str, default_type: Optional[str] = None) -> List[Term]:
    out: List[Term] = []
    if not isinstance(values, list):
        return out
    for i, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            continue
        out.append(
            Term(
                display=display_term(value),
                norm=normalize_term(value),
                field=f"{field_name}[{i}]",
                raw_type=default_type,
            )
        )
    return out


def _terms_from_tags(tags, tag_type: str) -> Tuple[List[Term], int]:
    """Return (terms, malformed_count) for one tag type."""
    out: List[Term] = []
    malformed = 0
    if not isinstance(tags, list):
        return out, 0
    for i, tag in enumerate(tags):
        if not isinstance(tag, dict):
            malformed += 1
            continue
        if tag.get("type") != tag_type:
            continue
        name = tag.get("name")
        if not isinstance(name, str) or not name.strip():
            malformed += 1
            continue
        ident = tag.get("id") or tag.get("value") or tag_type
        out.append(
            Term(
                display=display_term(name),
                norm=normalize_term(name),
                field=f"tags[{ident}].name",
                raw_type=tag_type,
            )
        )
    return out, malformed


def _dedupe(terms: List[Term]) -> Tuple[List[Term], int]:
    """Drop repeated normalized terms, keeping first occurrence."""
    seen = set()
    unique: List[Term] = []
    collapsed = 0
    for term in terms:
        if term.norm in seen:
            collapsed += 1
            continue
        seen.add(term.norm)
        unique.append(term)
    return unique, collapsed


def extract_profile(entity: dict) -> Profile:
    """Build a Profile from one Qloo entity payload.

    Raises ValueError if the payload is not a mapping or lacks an id.
    """
    if not isinstance(entity, dict):
        raise ValueError("entity payload must be a mapping")
    entity_id = entity.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        raise ValueError("entity payload has no entity_id")

    properties = entity.get("properties") if isinstance(entity.get("properties"), dict) else {}
    tags = entity.get("tags") if isinstance(entity.get("tags"), list) else []
    missing: List[str] = []

    genres = _terms_from_list(
        (properties.get("primary_genres") or []), "properties.primary_genres"
    ) + _terms_from_list(
        (properties.get("secondary_genres") or []), "properties.secondary_genres"
    )
    if not genres:
        missing.append("genres")

    themes = _terms_from_list((properties.get("lyrical_themes") or []), "properties.lyrical_themes")
    themes_tag_fallback = False
    if not themes:
        themes, _ = _terms_from_tags(tags, THEME_TAG)
        themes_tag_fallback = bool(themes)
    if not themes:
        missing.append("themes")

    audience, _ = _terms_from_tags(tags, AUDIENCE_TAG)
    if not audience:
        missing.append("audience")

    style, _ = _terms_from_tags(tags, STYLE_TAG)
    if not style:
        missing.append("style")

    instruments = _terms_from_list((properties.get("instruments") or []), "properties.instruments")
    influenced = _terms_from_list((properties.get("influenced_by") or []), "properties.influenced_by")

    dims = {
        "genres": genres,
        "themes": themes,
        "audience": audience,
        "style": style,
    }
    deduped: Dict[str, List[Term]] = {}
    duplicates: Dict[str, int] = {}
    for name, terms in dims.items():
        deduped[name], duplicates[name] = _dedupe(terms)
    if themes_tag_fallback:
        deduped["themes"] = [
            Term(t.display, t.norm, f"tags[{THEME_TAG}].name", THEME_TAG) for t in deduped["themes"]
        ]

    return Profile(
        entity_id=entity_id,
        name=str(entity.get("name") or entity_id),
        dimensions=deduped,
        display_only={
            "instruments": _dedupe(instruments)[0],
            "influenced_by": _dedupe(influenced)[0],
        },
        duplicates=duplicates,
        missing=missing,
        tag_count=len(tags),
        property_count=len(properties),
    )


def _status_for(overlap: Optional[float], config: AuditConfig) -> str:
    if overlap is None:
        return "unverifiable"
    if overlap >= config.strong_threshold:
        return "corroborated"
    if overlap > config.weak_threshold:
        return "weak"
    return "unsupported"


def compare_dimension(
    dimension: str,
    seed_terms: List[Term],
    candidate_terms: List[Term],
    config: AuditConfig,
    duplicates: int = 0,
) -> DimensionResult:
    """Compare one dimension and produce a self-describing receipt.

    `duplicates` carries collapses already performed at extraction time; the
    receipt reports them so a limitation can say duplicates were dropped.
    """
    seed_by_norm = {t.norm: t for t in seed_terms}
    cand_by_norm = {t.norm: t for t in candidate_terms}
    duplicates += (
        (len(seed_terms) - len(seed_by_norm)) + (len(candidate_terms) - len(cand_by_norm))
    )

    if not seed_by_norm and not cand_by_norm:
        return DimensionResult(
            dimension=dimension, status="unverifiable", overlap=None,
            seed_terms=[], candidate_terms=[], shared_terms=[],
            seed_sources=[], candidate_sources=[], duplicates_collapsed=duplicates,
            reason="neither entity has data in this dimension.",
        )
    if not seed_by_norm:
        return DimensionResult(
            dimension=dimension, status="unverifiable", overlap=None,
            seed_terms=[], candidate_terms=[t.display for t in cand_by_norm.values()],
            shared_terms=[], seed_sources=[],
            candidate_sources=[t.field for t in cand_by_norm.values()],
            duplicates_collapsed=duplicates,
            reason="the seed entity has no data in this dimension, so it cannot be compared.",
        )
    if not cand_by_norm:
        return DimensionResult(
            dimension=dimension, status="unverifiable", overlap=None,
            seed_terms=[t.display for t in seed_by_norm.values()], candidate_terms=[],
            shared_terms=[], seed_sources=[t.field for t in seed_by_norm.values()],
            candidate_sources=[], duplicates_collapsed=duplicates,
            reason="the candidate entity has no data in this dimension, so it cannot be compared.",
        )

    shared_norms = [n for n in seed_by_norm if n in cand_by_norm]
    overlap = len(shared_norms) / min(len(seed_by_norm), len(cand_by_norm))
    status = _status_for(overlap, config)

    if status == "corroborated":
        reason = (
            f"{len(shared_norms)} shared term(s); overlap {overlap:.2f} meets the "
            f"{config.strong_threshold:.2f} threshold."
        )
    elif status == "weak":
        reason = (
            f"{len(shared_norms)} shared term(s); overlap {overlap:.2f} is above zero "
            f"but below the {config.strong_threshold:.2f} threshold."
        )
    else:
        reason = "the two entities share no terms in this dimension."

    return DimensionResult(
        dimension=dimension,
        status=status,
        overlap=overlap,
        seed_terms=[t.display for t in seed_by_norm.values()],
        candidate_terms=[t.display for t in cand_by_norm.values()],
        shared_terms=[seed_by_norm[n].display for n in shared_norms],
        seed_sources=[seed_by_norm[n].field for n in shared_norms],
        candidate_sources=[cand_by_norm[n].field for n in shared_norms],
        duplicates_collapsed=duplicates,
        reason=reason,
    )


def compare_profiles(seed: Profile, candidate: Profile, config: AuditConfig) -> Dict[str, DimensionResult]:
    return {
        dim: compare_dimension(
            dim,
            seed.dimensions.get(dim) or [],
            candidate.dimensions.get(dim) or [],
            config,
            duplicates=seed.duplicates.get(dim, 0) + candidate.duplicates.get(dim, 0),
        )
        for dim in SCORED_DIMENSIONS
    }


def verify_receipts(seed_payload: dict, candidate_payload: dict, result) -> List[str]:
    """Re-derive every receipt from the raw payloads and report mismatches.

    This is the agent's self-check step: a receipt that cannot be reproduced
    from the source payload is a fabrication, and the audit is void.
    """
    problems: List[str] = []
    try:
        seed = extract_profile(seed_payload)
        cand = extract_profile(candidate_payload)
    except ValueError as exc:
        return [f"payload could not be re-read for verification: {exc}"]

    for dim, receipt in result.dimensions.items():
        if receipt.status == "unverifiable":
            continue
        seed_norms = {t.norm for t in seed.dimensions.get(dim) or []}
        cand_norms = {t.norm for t in cand.dimensions.get(dim) or []}
        expected_shared = seed_norms & cand_norms
        reported_shared = {normalize_term(t) for t in receipt.shared_terms}
        if reported_shared != expected_shared:
            problems.append(
                f"{dim}: reported shared terms {sorted(reported_shared)} do not match "
                f"payload {sorted(expected_shared)}"
            )
        if receipt.overlap is not None and expected_shared:
            recomputed = len(expected_shared) / min(len(seed_norms), len(cand_norms))
            if abs(recomputed - receipt.overlap) > 1e-9:
                problems.append(
                    f"{dim}: overlap {receipt.overlap} != recomputed {recomputed}"
                )
    return problems
