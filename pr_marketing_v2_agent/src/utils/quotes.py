"""Verbatim quote matching: a claim's quote must really appear in the evidence it cites.

Comparison ignores case, whitespace, markdown emphasis and typographic quotes/dashes, so a
model that re-wraps a line or straightens a quote still matches; anything more is a miss.
"""

import re
import unicodedata

MIN_QUOTE_CHARS = 12

_TRANSLATE = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "…": "...",
        " ": " ",
    }
)
_MARKUP_RE = re.compile(r"[*_`]+")
_SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_TRANSLATE)
    text = _MARKUP_RE.sub("", text)
    text = text.strip().strip("\"'").strip()
    return _SPACE_RE.sub(" ", text).lower()


def quote_in(quote: str, text: str, min_chars: int = MIN_QUOTE_CHARS) -> bool:
    """True when `quote` (long enough to mean something) appears in `text`."""
    q = normalize(quote)
    if q.endswith("..."):
        q = q[:-3].rstrip()
    t = normalize(text)
    return bool(q) and (len(q) >= min_chars or q == t) and q in t
