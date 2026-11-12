"""Deterministic routing: which features and components an event mentions by name, alias or
doc URL. The LLM router sees these as hints and may add to them; it never sees the whole
catalog's detail."""

import re
from typing import Any

MIN_ALIAS_CHARS = 4
MAX_FEATURES = 4
MAX_COMPONENTS = 3


def _count(term: str, text: str) -> int:
    pattern = r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])"
    return len(re.findall(pattern, text))


def _event_text(event: dict[str, Any]) -> str:
    return "\n".join([event["title"], *(e["text"] for e in event["evidence"])]).lower()


def page_hits(event: dict[str, Any], features: list[dict[str, Any]]) -> list[str]:
    """Features that cite the changed docs page as a source: the strongest possible match."""
    page = (event.get("refs") or {}).get("docs_page")
    return [f["slug"] for f in features if page and page in f.get("sources", [])]


def feature_hits(event: dict[str, Any], index: dict[str, Any]) -> list[str]:
    """Feature slugs whose name, slug or alias the event text mentions, most mentions first."""
    text = _event_text(event)
    scores: dict[str, int] = {}
    for f in index["features"]:
        terms = {f["name"], f["slug"].replace("-", " ")}
        terms |= {a for a in f["aliases"] if len(a) >= MIN_ALIAS_CHARS}
        hits = sum(_count(t, text) for t in terms if t)
        if hits:
            scores[f["slug"]] = hits
    return sorted(scores, key=lambda s: -scores[s])


def component_hits(event: dict[str, Any], architecture: dict[str, Any]) -> list[str]:
    text = _event_text(event)
    scores = {}
    for c in architecture["components"]:
        hits = sum(_count(t, text) for t in {c["name"], c["slug"].replace("-", " ")})
        if hits:
            scores[c["slug"]] = hits
    return sorted(scores, key=lambda s: -scores[s])


def merge(*ranked: list[str], known: set[str], limit: int) -> list[str]:
    """Several rankings in priority order, deduplicated and limited to known slugs."""
    return [s for s in dict.fromkeys(s for r in ranked for s in r) if s in known][:limit]


def subjects(
    route: dict[str, Any], index: dict[str, Any], architecture: dict[str, Any]
) -> list[dict[str, str]]:
    """What claims may be about, for the extract prompt."""
    names = {f["slug"]: f["name"] for f in index["features"]}
    roles = {c["slug"]: c["name"] for c in architecture["components"]}
    out = [{"about": s, "label": names[s]} for s in route["features"] if s in names]
    out += [
        {"about": f"component:{s}", "label": roles[s]} for s in route["components"] if s in roles
    ]
    if route.get("new_feature"):
        out.append({"about": f"new:{route['new_feature']}", "label": "a new capability"})
    return out
