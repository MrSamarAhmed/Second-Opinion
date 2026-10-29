"""Hand-authored seed inputs for the public demo.

Only artist names and a short human-written note live here. There are no entity
ids, no tag counts, no affinities and no captured response payloads: every seed
is resolved through the live Qloo API when it is selected. Keeping this list
hand-written is what lets a clean checkout run without captured data files.

The notes are editorial descriptions, not values returned by Qloo.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

SEED_CATALOG: Tuple[Tuple[str, str], ...] = (
    ("Bad Bunny", "reggaeton and Latin trap"),
    ("Charli XCX", "electropop"),
    ("Dua Lipa", "dance pop"),
    ("Fontaines D.C.", "post-punk"),
    ("Fred again..", "electronic and house"),
    ("Laufey", "jazz-pop"),
    ("Little Simz", "UK hip hop"),
    ("Sierra Ferrell", "Americana and folk"),
)


def seed_list() -> List[Dict[str, str]]:
    """The payload served by /api/seeds: names and labels only."""
    return [{"name": name, "label": name, "note": note} for name, note in SEED_CATALOG]
