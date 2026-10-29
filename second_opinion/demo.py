"""A synthetic seed set, so the real agent can be demonstrated without Qloo.

Everything in here is invented: the artist names, the entity ids, the genres, the
themes, the audience and style tags and the affinity numbers. None of it came
from Qloo, from a captured response, or from any real artist. It exists so that a
clean checkout — no `data/`, no credential, no network — can still show a judge
exactly what the auditor does, including the bounded follow-up, and so that no
demonstration has to borrow evidence from a live run to look convincing.

What is *not* synthetic is everything that reads these records: the extraction,
the overlap arithmetic, the state rule, the receipt re-derivation and the
investigation pass are the same code paths a live audit takes. The shapes below
follow the documented payload structure (`entity_id`, `name`, `properties.*`
lists, `tags[]` with a `type` and an `id`), which is what makes the receipts this
demo produces resolve against the records they claim to come from.

The adapter reports `mode = "synthetic"`, and the server serves it on a route of
its own (`/api/demo`) so that no response can be mistaken for a live Qloo audit,
by a reader or by a test.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .adapters import EntityResolutionError, SeedRef

DEMO_SEED = "Northbound Echo"

# Two scripted outcomes, both honest, chosen by which recommendation carries the
# highest affinity — and therefore which gap the single follow-up query is spent
# on. `improves` shows a gap that a direct re-read closes; `confirms` shows one
# that a direct re-read does not close.
SCENARIOS = ("improves", "confirms")

SEED_ID = "demo-entity-0001"


def _record(
    entity_id: str,
    name: str,
    genres: Tuple[str, ...] = (),
    secondary: Tuple[str, ...] = (),
    themes: Tuple[str, ...] = (),
    audience: Tuple[str, ...] = (),
    style: Tuple[str, ...] = (),
    instruments: Tuple[str, ...] = (),
    influenced: Tuple[str, ...] = (),
) -> dict:
    """One entity record in the documented shape.

    Tag ids are stable and human-readable because a receipt's provenance is the
    tag id: `tags[demo-audience-1].name` has to resolve against this payload for
    the audit to be worth anything.
    """
    tags = []
    for index, term in enumerate(audience, start=1):
        tags.append({
            "id": f"demo-audience-{index}",
            "type": "urn:tag:audience:qloo",
            "name": term,
        })
    for index, term in enumerate(style, start=1):
        tags.append({
            "id": f"demo-style-{index}",
            "type": "urn:tag:style:qloo",
            "name": term,
        })
    return {
        "entity_id": entity_id,
        "name": name,
        "properties": {
            "primary_genres": list(genres),
            "secondary_genres": list(secondary),
            "lyrical_themes": list(themes),
            "instruments": list(instruments),
            "influenced_by": list(influenced),
        },
        "tags": tags,
    }


# The seed. Populated in every scored dimension, so a candidate that looks thin
# is thin on its own side and the comparison is not one-sided by accident.
SEED_RECORD = _record(
    SEED_ID,
    DEMO_SEED,
    genres=("indie folk", "folk"),
    secondary=("chamber pop",),
    themes=("longing", "night travel", "small towns"),
    audience=("folk traditionalists", "late-night listeners"),
    style=("intimate", "warm"),
    instruments=("Acoustic Guitar", "Vocals"),
    influenced=("Wren Hollow", "Saltmarsh Trio"),
)

# What each entity looks like *inside* the recommendation list: a smaller record,
# with no audience or style tags. This is the situation the investigation pass
# exists for — a verdict limited by how the evidence arrived, not by the evidence.
LIST_RECORDS: Dict[str, dict] = {
    "demo-entity-0002": _record(
        "demo-entity-0002", "Cassette Harbour",
        genres=("jangle pop",), themes=("longing",),
        instruments=("Twelve-string Guitar",),
    ),
    "demo-entity-0003": _record(
        "demo-entity-0003", "Velvet Ferry",
        genres=("sea shanty",), themes=("harbours",),
    ),
    "demo-entity-0004": _record(
        "demo-entity-0004", "Static Meadow",
        genres=("folk", "indie folk"), themes=("longing", "small towns"),
        audience=("late-night listeners",), style=("warm",),
    ),
    "demo-entity-0005": _record(
        "demo-entity-0005", "Neon Alibi",
        genres=("synthpop",), themes=("city lights",),
    ),
}

# What the same entities look like when fetched by their own id. Cassette Harbour
# gains audience and style terms; Velvet Ferry gains only an audience term, which
# is enough to make a dimension comparable and not enough to change its verdict.
DIRECT_RECORDS: Dict[str, dict] = {
    "demo-entity-0002": _record(
        "demo-entity-0002", "Cassette Harbour",
        genres=("jangle pop",), themes=("longing",),
        audience=("folk traditionalists", "arena pop listeners"),
        style=("intimate",),
        instruments=("Twelve-string Guitar",),
    ),
    "demo-entity-0003": _record(
        "demo-entity-0003", "Velvet Ferry",
        genres=("sea shanty",), themes=("harbours",),
        audience=("folk traditionalists",),
    ),
    "demo-entity-0004": _record(
        "demo-entity-0004", "Static Meadow",
        genres=("folk", "indie folk"), themes=("longing", "small towns"),
        audience=("late-night listeners",), style=("warm",),
    ),
    "demo-entity-0005": _record(
        "demo-entity-0005", "Neon Alibi",
        genres=("synthpop",), themes=("city lights",),
    ),
}

AFFINITIES: Dict[str, Dict[str, Optional[float]]] = {
    "improves": {
        "demo-entity-0002": 0.81,
        "demo-entity-0003": 0.74,
        "demo-entity-0004": 0.68,
        "demo-entity-0005": None,
    },
    # The same four records with one recommendation ranked higher: the gap that
    # now looks most worth checking is the one that does not close.
    "confirms": {
        "demo-entity-0002": 0.81,
        "demo-entity-0003": 0.86,
        "demo-entity-0004": 0.68,
        "demo-entity-0005": None,
    },
}

ORDER = ("demo-entity-0002", "demo-entity-0003", "demo-entity-0004", "demo-entity-0005")


class SyntheticAdapter:
    """The three adapter calls the agent makes, answered from the records above."""

    mode = "synthetic"

    def __init__(self, scenario: str = "improves"):
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown demo scenario {scenario!r}")
        self.scenario = scenario
        self.queries: List[str] = []

    def resolve_seed(self, query: str) -> SeedRef:
        self.queries.append("resolve_seed")
        if query.strip().casefold() != DEMO_SEED.casefold():
            raise EntityResolutionError(
                f"the synthetic demo contains one seed, {DEMO_SEED!r}; {query!r} is not in it"
            )
        return SeedRef(SEED_ID, DEMO_SEED)

    def candidates(self, seed_id: str) -> List[Tuple[dict, Optional[float]]]:
        self.queries.append("candidates")
        affinities = AFFINITIES[self.scenario]
        return [
            (LIST_RECORDS[entity_id], affinities.get(entity_id))
            for entity_id in ORDER
            if entity_id != seed_id
        ]

    def seed_payload(self, entity_id: str) -> Optional[dict]:
        # In the real adapters this is the by-id entity lookup, used for the seed
        # by the first pass and for one chosen candidate by the follow-up.
        self.queries.append("entity_record")
        if entity_id == SEED_ID:
            return SEED_RECORD
        return DIRECT_RECORDS.get(entity_id)


def demo_report(config=None) -> dict:
    """Run the real agent over the `improves` scenario."""
    from .agent import SecondOpinionAgent
    from .config import DEFAULT_CONFIG

    agent = SecondOpinionAgent(SyntheticAdapter("improves"), config=config or DEFAULT_CONFIG)
    return agent.audit(DEMO_SEED)
