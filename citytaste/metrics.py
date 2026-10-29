"""Core tile-set mathematics for CityTaste.

Every definition here is frozen before results are inspected. Nothing in this
module knows about artists, cities or HTTP: it operates on tile sets only, so it
can be tested with synthetic data.

Vocabulary
----------
U        : global universe = union of every tile across the loaded maps.
U_a      : eligible tiles for artist a = the tiles present in a's own response.
topN(a)  : the N tiles in U_a with the highest `affinity_rank`
           (ties broken by geohash, ascending, for determinism).
O_ab     : overlap coefficient = |topN(a) & topN(b)| / N   (denominator is N).

Null model
----------
Each artist's top-N is modelled as N tiles drawn uniformly without replacement
from that artist's own map, independently across artists. A tile absent from a
map is *never* treated as zero affinity; it is simply not eligible for that
artist.

    expected |A & B| = |U_a & U_b| * (N/|U_a|) * (N/|U_b|)
    expected O       = expected |A & B| / N
    lift             = O / expected O        (None when expected O == 0)

Pair-specific method (M4) restricts both selections to U_a & U_b:
    expected |A & B| = N^2 / |U_a & U_b|
    expected O       = N / |U_a & U_b|
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Sequence, Set


@dataclass(frozen=True)
class Tile:
    geohash: str
    rank: float
    lat: float
    lon: float


def load_tiles(payload: Mapping) -> Dict[str, Tile]:
    """Extract valid tiles from a saved /v2/insights heatmap response.

    Raises ValueError on duplicate geohashes (data integrity failure) and
    silently drops rows without a usable geohash or numeric affinity_rank.
    """
    rows = (payload.get("results") or {}).get("heatmap") or []
    tiles: Dict[str, Tile] = {}
    for row in rows:
        location = row.get("location") or {}
        query = row.get("query") or {}
        geohash = location.get("geohash")
        rank = query.get("affinity_rank")
        if not isinstance(geohash, str) or not geohash:
            continue
        if not isinstance(rank, (int, float)) or isinstance(rank, bool):
            continue
        if geohash in tiles:
            raise ValueError(f"duplicate geohash in response: {geohash}")
        tiles[geohash] = Tile(
            geohash=geohash,
            rank=float(rank),
            lat=float(location.get("latitude") or 0.0),
            lon=float(location.get("longitude") or 0.0),
        )
    return tiles


def top_n(tiles: Mapping[str, Tile], n: int, restrict_to: Optional[Set[str]] = None) -> Set[str]:
    """The N highest-ranked tiles, optionally restricted to a shared universe."""
    if n <= 0:
        raise ValueError("N must be positive")
    pool = tiles.keys() if restrict_to is None else (tiles.keys() & restrict_to)
    ordered = sorted(pool, key=lambda g: (-tiles[g].rank, g))
    return set(ordered[:n])


def overlap(ranked: Set[str], other: Set[str], n: int) -> float:
    return len(ranked & other) / n


@dataclass(frozen=True)
class Comparison:
    method: str
    n: int
    universe: int
    selected_a: int
    selected_b: int
    shared_selected: int
    overlap_coefficient: float
    expected_overlap: float
    lift: Optional[float]
    eligible_a: int
    eligible_b: int
    eligible_shared: int
    coverage_a: float
    coverage_b: float
    excluded: bool


def compare(
    tiles_a: Mapping[str, Tile],
    tiles_b: Mapping[str, Tile],
    n: int,
    method: str,
    universe: Set[str],
) -> Comparison:
    """Compare two artists' top-N sets under one of the four frozen methods.

    method="global": top-N drawn from each artist's own map.
    method="pair":   top-N drawn from the pair's shared tiles.
    """
    if method not in ("global", "pair"):
        raise ValueError(f"unknown method: {method}")

    ua, ub = set(tiles_a), set(tiles_b)
    shared = ua & ub
    excluded = False

    if method == "global":
        sel_a = top_n(tiles_a, n)
        sel_b = top_n(tiles_b, n)
        expected = (len(shared) * (n / len(ua)) * (n / len(ub))) / n if ua and ub else 0.0
        if len(ua) < n or len(ub) < n:
            excluded = True
    else:
        if len(shared) < n:
            # Not enough common tiles to build two comparable sets.
            excluded = True
            sel_a = sel_b = set()
            expected = 0.0
        else:
            sel_a = top_n(tiles_a, n, restrict_to=shared)
            sel_b = top_n(tiles_b, n, restrict_to=shared)
            expected = n / len(shared)

    observed = len(sel_a & sel_b) / n
    lift = (observed / expected) if expected > 0 else None

    return Comparison(
        method=method,
        n=n,
        universe=len(universe),
        selected_a=len(sel_a),
        selected_b=len(sel_b),
        shared_selected=len(sel_a & sel_b),
        overlap_coefficient=observed,
        expected_overlap=expected,
        lift=lift,
        eligible_a=len(ua),
        eligible_b=len(ub),
        eligible_shared=len(shared),
        coverage_a=len(ua) / len(universe) if universe else 0.0,
        coverage_b=len(ub) / len(universe) if universe else 0.0,
        excluded=excluded,
    )


METHODS = (("global", 10), ("global", 20), ("global", 30), ("pair", 30))


def evaluate_bill(
    tiles: Mapping[str, Mapping[str, Tile]],
    bill: Sequence[str],
    universe: Set[str],
) -> Dict[tuple, Comparison]:
    """Max pairwise comparison for each of the four methods for one bill."""
    from itertools import combinations

    result = {}
    for method, n in METHODS:
        worst = None
        for a, b in combinations(bill, 2):
            c = compare(tiles[a], tiles[b], n, method, universe)
            # Higher lift = more overlap than chance = more redundancy.
            # Undefined lift sorts below every defined lift.
            if worst is None:
                worst = c
            elif c.lift is not None and (worst.lift is None or c.lift > worst.lift):
                worst = c
        result[(method, n)] = worst
    return result


def max_lift(comps: Mapping[tuple, Comparison]) -> Dict[tuple, Optional[float]]:
    return {k: v.lift for k, v in comps.items()}
