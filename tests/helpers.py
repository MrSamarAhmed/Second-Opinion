"""Shared test helpers: payload loading, provenance resolution, rule oracle.

Two things live here that the rest of the suite depends on.

Captured responses are not published. `data/fixtures/**` holds Qloo API
responses and stays on the author's machine; on a checkout without them every
fixture-dependent test skips with an explicit reason rather than failing, so
`unittest discover` still reports a clean run.

Identifiers are synthetic. Tests refer to entities by the placeholder ids below
and resolve them to whatever record the local captures contain, matched by
artist name. No API-returned entity id is committed here; the adapters' real ids
are read at runtime instead of being asserted literally.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Dict, Iterator, Optional

from second_opinion.config import AuditConfig, DEFAULT_CONFIG

FIXTURES = Path(__file__).resolve().parent.parent / "data" / "fixtures"

# The captured Qloo responses are deliberately not published. On a checkout
# without them the fixture-dependent tests skip with an explicit reason instead
# of failing, so `unittest discover` still reports a clean run.
HAS_CAPTURED = (
    (FIXTURES / "pair").is_dir()
    and any((FIXTURES / "pair").glob("pair_*.json"))
    and (FIXTURES / "search").is_dir()
    and any((FIXTURES / "search").glob("search_*.json"))
)


def needs_captured(test_class):
    return unittest.skipUnless(
        HAS_CAPTURED, "captured Qloo fixtures are not present in this checkout"
    )(test_class)


# Synthetic stand-ins for entity identifiers. The key is what tests use; the
# artist name is public information and is what the lookup matches on when the
# local captures are present.
TEST_ENTITY_NAMES: Dict[str, str] = {
    "00000000-0000-4000-8000-0000000000a1": "Fontaines D.C.",
    "00000000-0000-4000-8000-0000000000b2": "Little Simz",
    "00000000-0000-4000-8000-0000000000c3": "RÜFÜS DU SOL",
    "00000000-0000-4000-8000-0000000000d4": "Bad Bunny",
    "00000000-0000-4000-8000-0000000000e5": "Dua Lipa",
}

_INDEXED = re.compile(r"^(?P<head>[A-Za-z_.]+)\[(?P<idx>\d+)\]$")
_TAG = re.compile(r"^tags\[(?P<ident>.+)\]\.name$")


def _test_name(identifier: str) -> str:
    return TEST_ENTITY_NAMES.get(identifier, identifier)


def _same_name(value, wanted: str) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().casefold() == wanted.strip().casefold()


def load_json(relpath: str) -> dict:
    return json.loads((FIXTURES / relpath).read_text())


def _iter_pair_records() -> Iterator[dict]:
    for path in sorted((FIXTURES / "pair").glob("pair_*.json")):
        for candidate in json.loads(path.read_text())["results"]["entities"]:
            yield candidate


def _iter_search_records() -> Iterator[dict]:
    for path in sorted((FIXTURES / "search").glob("search_*.json")):
        results = json.loads(path.read_text()).get("results") or []
        if results:
            yield results[0]


def entity(identifier: str) -> dict:
    """Return the richest saved record for a test alias.

    Records are matched by the artist name behind the alias (or by a literal id,
    when a caller passes one), which keeps API-returned ids out of the tests.
    Candidate lists are searched first, then the seed records, mirroring how the
    fixture adapter falls back to the search record.
    """
    wanted = _test_name(identifier)
    best = None
    for record in _iter_pair_records():
        if record.get("entity_id") == identifier or _same_name(record.get("name"), wanted):
            if best is None or len(record.get("tags") or []) > len(best.get("tags") or []):
                best = record
    if best is None:
        for record in _iter_search_records():
            if record.get("entity_id") == identifier or _same_name(record.get("name"), wanted):
                best = record
                break
    if best is None:
        raise LookupError(identifier)
    return best


def captured_id(identifier: str) -> str:
    """Return the entity id the adapters report for a test alias, read at runtime."""
    return entity(identifier)["entity_id"]


def seed_entity(name_fragment: str) -> dict:
    for record in _iter_search_records():
        if name_fragment.lower() in (record.get("name") or "").lower():
            return record
    raise LookupError(name_fragment)


def resolve_field(payload: dict, path: str):
    """Resolve a receipt provenance path against the raw payload.

    Supports "properties.lyrical_themes[3]", "properties.primary_genres[0]"
    and "tags[urn:tag:audience:qloo:loyal].name". Raises KeyError when the path
    does not exist in the payload, which is exactly what a fabricated receipt
    would do.
    """
    indexed = _INDEXED.match(path)
    if indexed:
        head, idx = indexed.group("head"), int(indexed.group("idx"))
        node = payload
        for part in head.split("."):
            node = node[part]
        value = node[idx]
        if not isinstance(value, str):
            raise KeyError(f"{path} does not resolve to a string")
        return value

    tagged = _TAG.match(path)
    if tagged:
        ident = tagged.group("ident")
        for tag in payload.get("tags") or []:
            if tag.get("id") == ident:
                value = tag.get("name")
                if not isinstance(value, str):
                    raise KeyError(f"{path} tag has no name")
                return value
        raise KeyError(f"{path} not found in payload")

    raise KeyError(f"unsupported provenance path: {path}")


def expected_dimension_status(
    overlap: Optional[float], config: AuditConfig = DEFAULT_CONFIG
) -> str:
    """Independent restatement of the per-dimension status rule.

    Written from the rule as documented in the README rather than by calling
    production code, so it can serve as an oracle against `compare_dimension`.
    """
    if overlap is None:
        return "unverifiable"
    if overlap >= config.strong_threshold:
        return "corroborated"
    if overlap > config.weak_threshold:
        return "weak"
    return "unsupported"


def expected_state(
    affinity: Optional[float],
    statuses: Dict[str, str],
    seed_usable: bool,
    candidate_usable: bool,
    config: AuditConfig = DEFAULT_CONFIG,
) -> str:
    """Independent restatement of the decision rule and its precedence."""
    if not seed_usable or not candidate_usable:
        return "UNVERIFIABLE"
    if affinity is None:
        return "UNVERIFIABLE"
    evaluable = sum(1 for status in statuses.values() if status != "unverifiable")
    corroborated = sum(1 for status in statuses.values() if status == "corroborated")
    if evaluable < config.min_dimensions_evaluable:
        return "UNVERIFIABLE"
    if corroborated >= config.min_dimensions_corroborated:
        return "CORROBORATED"
    return "AFFINITY_ONLY"
