"""The feature catalog: which features exist, their Memsy actor, and which fact versions are
current. Stored as one JSON object in SDK storage (knowledge/catalog.json).

Memsy can't edit or delete memories, so a changed fact is ingested again with a higher
`fact_v`, and search results are kept only when every source event is the version the
catalog lists (see `is_current`). Only Elephant writes the catalog, one change at a time.

Functions here are pure (dict in, dict out) except `load` and `save`.
"""

import copy
import json
import os
import re
from typing import Any

from utils import storage

KNOWLEDGE_PREFIX = "product_knowledge"
CATALOG_KEY = f"{KNOWLEDGE_PREFIX}/catalog.json"

# Memsy's searchable-id charset is [A-Za-z0-9_-:.@/], at most 256 chars. Slugs are stricter
# so they stay readable and never contain the ":" separator.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FACT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)+$")
MAX_ID_CHARS = 256

STATES = ("published", "withdrawn")


def product_id() -> str:
    return os.getenv("PRODUCT_ID", "aetherion")


def slugify(name: str) -> str:
    """'Slack Connector (v2)' -> 'slack-connector-v2'."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if not slug:
        raise ValueError(f"Can't make a feature slug from {name!r}.")
    return slug


def check_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug or ""):
        raise ValueError(f"Bad feature slug {slug!r}: use lowercase letters, digits and '-'.")
    return slug


def check_fact_id(fact_id: str, slug: str) -> str:
    """Fact ids are '<slug>.<section>.<name>', e.g. 'slack-connector.limits.max-rows'."""
    if not _FACT_ID_RE.match(fact_id or "") or not fact_id.startswith(f"{slug}."):
        raise ValueError(f"Bad fact id {fact_id!r}: expected '{slug}.<section>.<name>'.")
    if len(fact_id) > MAX_ID_CHARS:
        raise ValueError(f"Fact id longer than {MAX_ID_CHARS} characters: {fact_id!r}")
    return fact_id


def actor_id(slug: str, product: str | None = None) -> str:
    """The one Memsy actor for a feature: 'feature:<product>:<slug>'. Never versioned."""
    product = check_slug(product or product_id())
    value = f"feature:{product}:{check_slug(slug)}"
    if len(value) > MAX_ID_CHARS:
        raise ValueError(f"Actor id longer than {MAX_ID_CHARS} characters: {value!r}")
    return value


def empty(product: str | None = None) -> dict[str, Any]:
    return {"product": product or product_id(), "updated_at": None, "features": {}}


def load() -> dict[str, Any]:
    raw = storage.get(CATALOG_KEY)
    return json.loads(raw) if raw else empty()


def save(catalog: dict[str, Any], at: str) -> dict[str, Any]:
    saved = {**catalog, "updated_at": at}
    storage.put(
        CATALOG_KEY, json.dumps(saved, ensure_ascii=False, indent=1).encode(), "application/json"
    )
    return saved


def upsert_feature(
    catalog: dict[str, Any],
    slug: str,
    *,
    name: str,
    summary: str,
    aliases: list[str] | None = None,
    overview_sections: list[str] | None = None,
) -> dict[str, Any]:
    """Add a feature, or update its name/summary/aliases. The slug (and actor) never change;
    an old name passed as `name` is kept as an alias so lookups by it still work."""
    out = copy.deepcopy(catalog)
    check_slug(slug)
    entry = out["features"].get(slug)
    if entry is None:
        entry = {
            "name": name,
            "aliases": [],
            "summary": summary,
            "actor_id": actor_id(slug, out["product"]),
            "state": "published",
            "overview_sections": [],
            "facts": {},
            "changes": [],
        }
    elif entry["name"] != name:
        entry["aliases"] = [entry["name"], *entry["aliases"]]
    entry["name"] = name
    entry["summary"] = summary
    entry["aliases"] = list(
        dict.fromkeys(a for a in [*entry["aliases"], *(aliases or [])] if a and a != name)
    )
    if overview_sections is not None:
        entry["overview_sections"] = list(dict.fromkeys(overview_sections))
    out["features"][slug] = entry
    return out


def feature(catalog: dict[str, Any], slug: str) -> dict[str, Any]:
    entry = catalog["features"].get(slug)
    if entry is None:
        raise ValueError(f"No feature {slug!r} in the catalog.")
    return entry


def next_version(catalog: dict[str, Any], slug: str, fact_id: str) -> int:
    """Version to ingest a new or changed fact with. Withdrawn facts restart above their
    last version, so an old event can never become current again by accident."""
    entry = feature(catalog, slug)
    last = max(
        [entry["facts"].get(fact_id, 0)]
        + [
            c.get("to_v") or c.get("from_v") or 0
            for c in entry["changes"]
            if c["fact_id"] == fact_id
        ]
    )
    return last + 1


def set_facts(
    catalog: dict[str, Any],
    slug: str,
    versions: dict[str, int],
    *,
    at: str,
    source: str,
) -> dict[str, Any]:
    """Point the catalog at new fact versions (after Memsy has finished processing them)."""
    out = copy.deepcopy(catalog)
    entry = feature(out, slug)
    for fact_id, version in versions.items():
        check_fact_id(fact_id, slug)
        current = entry["facts"].get(fact_id)
        if current == version:
            continue  # already applied: re-running a PR changes nothing
        if not isinstance(version, int) or version < 1 or (current and version < current):
            raise ValueError(f"{fact_id}: can't move from version {current} to {version}.")
        entry["facts"][fact_id] = version
        entry["changes"].append(
            {"at": at, "source": source, "fact_id": fact_id, "from_v": current, "to_v": version}
        )
    return out


def withdraw_facts(
    catalog: dict[str, Any], slug: str, fact_ids: list[str], *, at: str, source: str
) -> dict[str, Any]:
    """Stop returning these facts. Their events stay in Memsy (it can't delete) but no
    longer pass `is_current`."""
    out = copy.deepcopy(catalog)
    entry = feature(out, slug)
    for fact_id in fact_ids:
        if fact_id in entry["facts"]:
            entry["changes"].append(
                {
                    "at": at,
                    "source": source,
                    "fact_id": fact_id,
                    "from_v": entry["facts"].pop(fact_id),
                    "to_v": None,
                }
            )
    return out


def set_state(catalog: dict[str, Any], slug: str, state: str) -> dict[str, Any]:
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    out = copy.deepcopy(catalog)
    feature(out, slug)["state"] = state
    return out


def is_current(catalog: dict[str, Any], meta: dict[str, Any] | None) -> bool:
    """True when an event's metadata names a published feature's current fact version."""
    if not isinstance(meta, dict):
        return False
    entry = catalog["features"].get(meta.get("feature"))
    if not entry or entry["state"] != "published":
        return False
    return entry["facts"].get(meta.get("fact_id")) == meta.get("fact_v")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def find(catalog: dict[str, Any], text: str, limit: int = 5) -> list[dict[str, Any]]:
    """Published features whose slug, name or alias appears in `text` (or contains it).
    Longest match first, so 'slack connector' beats 'connector'."""
    query = _norm(text)
    if not query:
        return []
    hits = []
    for slug, entry in catalog["features"].items():
        if entry["state"] != "published":
            continue
        names = [_norm(n) for n in [slug, entry["name"], *entry["aliases"]]]
        matched = [n for n in names if n and (f" {n} " in f" {query} " or query in n)]
        if matched:
            hits.append((max(len(n) for n in matched), slug, entry))
    hits.sort(key=lambda h: (-h[0], h[1]))
    return [
        {"slug": slug, "name": e["name"], "summary": e["summary"], "actor_id": e["actor_id"]}
        for _, slug, e in hits[:limit]
    ]


def listing(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Every published feature, for the model to choose from."""
    return [
        {"slug": slug, "name": e["name"], "aliases": e["aliases"], "summary": e["summary"]}
        for slug, e in sorted(catalog["features"].items())
        if e["state"] == "published"
    ]
