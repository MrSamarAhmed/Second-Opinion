"""Deterministic term normalization.

The only requirement is that the same cultural term written two different ways
compares equal: "Dance-Pop", "Dance Pop" and "dance pop" are one term, and
"R&B" is "r and b". Nothing here infers meaning, synonyms or stemming — that
would turn matching into a guess.
"""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^a-z0-9 ]+")

# Applied before punctuation stripping, so "&" does not vanish silently.
_AMPERSAND = re.compile(r"\s*&\s*")
_SLASH = re.compile(r"\s*/\s*")


def normalize_term(raw: str) -> str:
    """Normalize one term to a comparison key.

    Steps, in order: Unicode NFKD with combining marks removed (so "RÜFÜS" is
    "rufus"), lowercase, "&" -> "and", "/" -> space, "-"/"_" -> space, drop
    remaining non-alphanumerics, collapse whitespace.

    Diacritic folding is spelling-only: it lets "Música" match "Musica" without
    asserting that the two labels mean the same thing. Deliberately does NOT
    stem, drop stopwords, or apply synonyms: "Rap" and "Rapping" stay distinct
    so a shared tag is never manufactured.
    """
    if not isinstance(raw, str):
        raise TypeError("term must be a string")
    decomposed = unicodedata.normalize("NFKD", raw)
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    text = folded.strip().lower()
    text = _AMPERSAND.sub(" and ", text)
    text = _SLASH.sub(" ", text)
    text = text.replace("-", " ").replace("_", " ")
    text = _NON_WORD.sub(" ", text)
    return _WS.sub(" ", text).strip()


def display_term(raw: str) -> str:
    """Human-facing form: original spelling, whitespace collapsed."""
    return _WS.sub(" ", str(raw)).strip()
