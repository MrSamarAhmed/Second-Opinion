"""Data structures shared by the engine, the agent and the HTTP layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Term:
    """One normalized evidence term plus the exact place it came from."""

    display: str
    norm: str
    field: str          # e.g. "properties.primary_genres[2]"
    raw_type: Optional[str] = None  # tag type, when the term came from tags[]


@dataclass
class Profile:
    """All extractable metadata for one entity."""

    entity_id: str
    name: str
    dimensions: Dict[str, List[Term]] = field(default_factory=dict)
    display_only: Dict[str, List[Term]] = field(default_factory=dict)
    duplicates: Dict[str, int] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    tag_count: int = 0
    property_count: int = 0

    def is_usable(self) -> bool:
        """True when at least one scored dimension has a term."""
        return any(self.dimensions.get(d) for d in self.dimensions)


@dataclass(frozen=True)
class DimensionResult:
    dimension: str
    status: str                 # corroborated | weak | unsupported | unverifiable
    overlap: Optional[float]
    seed_terms: List[str]
    candidate_terms: List[str]
    shared_terms: List[str]
    seed_sources: List[str]
    candidate_sources: List[str]
    duplicates_collapsed: int
    reason: str


@dataclass
class AuditResult:
    seed: str
    seed_id: str
    candidate: str
    candidate_id: str
    affinity: Optional[float]
    state: str
    reason: str
    dimensions: Dict[str, DimensionResult]
    display_only: Dict[str, List[str]]
    limitations: List[str]
    receipts_verified: bool
    verification_problems: List[str]
    config: Dict[str, float]

    def to_dict(self) -> dict:
        return {
            "seed": self.seed,
            "seed_id": self.seed_id,
            "candidate": self.candidate,
            "candidate_id": self.candidate_id,
            "affinity": self.affinity,
            "state": self.state,
            "reason": self.reason,
            "dimensions": {
                name: {
                    "dimension": r.dimension,
                    "status": r.status,
                    "overlap": r.overlap,
                    "seed_terms": r.seed_terms,
                    "candidate_terms": r.candidate_terms,
                    "shared_terms": r.shared_terms,
                    "seed_sources": r.seed_sources,
                    "candidate_sources": r.candidate_sources,
                    "duplicates_collapsed": r.duplicates_collapsed,
                    "reason": r.reason,
                }
                for name, r in self.dimensions.items()
            },
            "display_only": self.display_only,
            "limitations": self.limitations,
            "receipts_verified": self.receipts_verified,
            "verification_problems": self.verification_problems,
            "config": self.config,
        }
