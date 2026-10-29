"""Seed/candidate adapters.

Two adapters behind one interface:

  FixtureAdapter  reads `data/fixtures/**` only. No network, no credentials.
  LiveAdapter     calls the Qloo API with `QLOO_API_KEY` from the environment.

The fixture adapter is what the recorded tests run on; it proves nothing about
the API. Live mode requires `QLOO_API_KEY` in the environment and is verified by
an authenticated call made by whoever runs it — the tests here exercise live mode
against stubs so they never touch the network.
"""

from __future__ import annotations

import json
import os
import socket
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .normalize import normalize_term


class AdapterError(Exception):
    """Base class for adapter failures."""


class MissingCredentials(AdapterError):
    pass


class AuthenticationError(AdapterError):
    pass


class UpstreamTimeout(AdapterError):
    pass


class MalformedResponse(AdapterError):
    pass


class EntityResolutionError(AdapterError):
    pass


def name_key(value: str) -> str:
    """Diacritic- and punctuation-insensitive key for entity names.

    "Fontaines_D.C." and "Fontaines D.C." both become "fontaines d c";
    "RÜFÜS DU SOL" becomes "rufus du sol".
    """
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return normalize_term(stripped)


class SeedRef:
    __slots__ = ("entity_id", "name", "label")

    def __init__(self, entity_id: str, name: str, label: Optional[str] = None):
        self.entity_id = entity_id
        self.name = name
        self.label = label or name


class FixtureAdapter:
    """Read-only access to the saved Qloo responses."""

    mode = "fixture"

    def __init__(self, fixture_root: Optional[Path] = None):
        root = Path(fixture_root) if fixture_root else Path(__file__).resolve().parent.parent / "data" / "fixtures"
        self.root = Path(root)
        self.pair_dir = self.root / "pair"
        self.search_dir = self.root / "search"
        if not self.pair_dir.is_dir() or not self.search_dir.is_dir():
            raise AdapterError(f"fixture directories not found under {self.root}")
        self._pair_files: List[Tuple[str, dict]] = []
        self._seeds: Dict[str, dict] = {}
        self._seed_by_key: Dict[str, List[str]] = {}
        self._registry: Dict[str, dict] = {}
        self._seed_to_pair: Dict[str, dict] = {}
        self._load()

    # -- loading ---------------------------------------------------------
    def _load(self) -> None:
        for path in sorted(self.search_dir.glob("search_*.json")):
            payload = json.loads(path.read_text())
            results = payload.get("results") or []
            if not results:
                continue
            entity = results[0]
            eid = entity.get("entity_id")
            if not eid:
                continue
            self._seeds[eid] = entity
            self._seed_by_key.setdefault(name_key(entity.get("name", "")), []).append(eid)

        for path in sorted(self.pair_dir.glob("pair_*.json")):
            payload = json.loads(path.read_text())
            entities = (payload.get("results") or {}).get("entities") or []
            self._pair_files.append((path.name, payload))
            for entity in entities:
                eid = entity.get("entity_id")
                if not eid:
                    continue
                current = self._registry.get(eid)
                # Keep the richest record deterministically.
                if current is None or len(entity.get("tags") or []) > len(current.get("tags") or []):
                    self._registry[eid] = entity

        for filename, payload in self._pair_files:
            entities = (payload.get("results") or {}).get("entities") or []
            cand_ids = {e.get("entity_id") for e in entities}
            seed_ids = [sid for sid in self._seeds if sid not in cand_ids]
            # The seed is the search entity absent from its own candidate list.
            map_name = name_key(filename[len("pair_"):-len(".json")])
            match = [sid for sid in seed_ids if name_key(self._seeds[sid].get("name", "")) == map_name]
            if len(match) == 1:
                self._seed_to_pair[match[0]] = payload
            elif len(seed_ids) == 1:
                self._seed_to_pair[seed_ids[0]] = payload

    # -- interface -------------------------------------------------------
    def list_seeds(self) -> List[SeedRef]:
        return [
            SeedRef(eid, e.get("name", eid), e.get("disambiguation"))
            for eid, e in sorted(self._seeds.items(), key=lambda kv: kv[1].get("name", ""))
        ]

    def resolve_seed(self, query: str) -> SeedRef:
        """Resolve a user-supplied seed. Fails loudly rather than guessing."""
        if not isinstance(query, str) or not query.strip():
            raise EntityResolutionError("empty seed query")
        if query in self._seeds:
            e = self._seeds[query]
            return SeedRef(query, e.get("name", query), e.get("disambiguation"))
        matches = self._seed_by_key.get(name_key(query), [])
        if len(matches) == 1:
            e = self._seeds[matches[0]]
            return SeedRef(matches[0], e.get("name", query), e.get("disambiguation"))
        if len(matches) > 1:
            raise EntityResolutionError(
                f"seed {query!r} is ambiguous: {len(matches)} fixtures match"
            )
        raise EntityResolutionError(f"no fixture seed matches {query!r}")

    def seed_payload(self, seed_id: str) -> Optional[dict]:
        """Rich entity record for the seed, if the fixtures contain one."""
        if seed_id in self._registry:
            return self._registry[seed_id]
        entity = self._seeds.get(seed_id)
        # Search records carry only five properties; still return them so the
        # auditor can report precisely which fields are missing.
        return entity

    def candidates(self, seed_id: str) -> List[Tuple[dict, Optional[float]]]:
        payload = self._seed_to_pair.get(seed_id)
        if payload is None:
            raise EntityResolutionError(
                f"no saved recommendation set exists for seed {seed_id!r}"
            )
        out = []
        for entity in (payload.get("results") or {}).get("entities") or []:
            affinity = (entity.get("query") or {}).get("affinity")
            out.append((entity, affinity))
        return out

    def seed_metadata_summary(self, seed_id: str) -> dict:
        entity = self._seeds.get(seed_id) or {}
        rich = self._registry.get(seed_id)
        return {
            "entity_id": seed_id,
            "name": entity.get("name"),
            "search_tags": len(entity.get("tags") or []),
            "search_properties": len(entity.get("properties") or {}),
            "registry_tags": len((rich or {}).get("tags") or []) if rich else 0,
            "has_rich_record": rich is not None,
        }


class LiveAdapter:
    """Minimal Qloo client. Reads QLOO_API_KEY from the environment only."""

    mode = "live"

    def __init__(self, base_url: Optional[str] = None, timeout: float = 12.0):
        self.base_url = (base_url or os.environ.get("QLOO_API_HOST") or "https://hackathon.api.qloo.com").rstrip("/")
        self.timeout = timeout
        self._key = os.environ.get("QLOO_API_KEY")
        if not self._key:
            raise MissingCredentials(
                "QLOO_API_KEY is not set; live mode requires an API key in the environment"
            )

    def _get(self, path: str, params: dict, allow_404: bool = False) -> dict:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"X-Api-Key": self._key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AuthenticationError(f"Qloo rejected the credential (HTTP {exc.code})") from None
            if exc.code == 404 and allow_404:
                # /search documents 404 as "no results", not a transport failure.
                return {}
            raise AdapterError(f"Qloo returned HTTP {exc.code}") from None
        except (TimeoutError, socket.timeout):
            raise UpstreamTimeout("request to Qloo timed out") from None
        except urllib.error.URLError as exc:
            # socket.timeout is only an alias of TimeoutError from Python 3.10.
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise UpstreamTimeout("request to Qloo timed out") from None
            raise AdapterError(f"could not reach Qloo: {exc.reason}") from None
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise MalformedResponse("Qloo returned a body that is not valid JSON") from None

    def resolve_seed(self, query: str) -> SeedRef:
        # `types` is the documented parameter; a singular `type` is silently
        # ignored by the API, which would return unfiltered results.
        payload = self._get(
            "/search",
            {"query": query, "types": "urn:entity:artist", "take": 5},
            allow_404=True,
        )
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            raise EntityResolutionError(f"Qloo returned no entity for {query!r}")
        best = results[0]
        eid = best.get("entity_id")
        if not eid:
            raise MalformedResponse("search result has no entity_id")
        return SeedRef(eid, best.get("name", query))

    def seed_payload(self, seed_id: str) -> dict:
        # `signal.interests.entities` never returns the seed itself, so asking
        # for take=1 there yields a *recommendation* and the audit would compare
        # the wrong artist. `filter.results.entities` with no signal is the
        # documented way to fetch one entity's full record.
        payload = self._get(
            "/v2/insights",
            {
                "filter.type": "urn:entity:artist",
                "filter.results.entities": seed_id,
                "take": 1,
            },
        )
        entities = (payload.get("results") or {}).get("entities") or []
        if not entities:
            raise EntityResolutionError("Qloo returned no entity record for the seed")
        record = entities[0]
        if record.get("entity_id") != seed_id:
            raise EntityResolutionError(
                "Qloo returned a different entity than the requested seed"
            )
        return record

    def candidates(self, seed_id: str) -> List[Tuple[dict, Optional[float]]]:
        payload = self._get(
            "/v2/insights",
            {"signal.interests.entities": seed_id, "take": 20, "filter.type": "urn:entity:artist"},
        )
        entities = (payload.get("results") or {}).get("entities")
        if not isinstance(entities, list):
            raise MalformedResponse("insights response has no results.entities list")
        out = []
        for entity in entities:
            if entity.get("entity_id") == seed_id:
                continue
            out.append((entity, (entity.get("query") or {}).get("affinity")))
        return out


def describe_environment() -> dict:
    """Report readiness without ever revealing the key."""
    key = os.environ.get("QLOO_API_KEY")
    host = os.environ.get("QLOO_API_HOST") or "https://hackathon.api.qloo.com"
    return {
        "live_ready": bool(key),
        "key_present": bool(key),
        "key_length": len(key) if key else 0,
        "host": host,
    }
