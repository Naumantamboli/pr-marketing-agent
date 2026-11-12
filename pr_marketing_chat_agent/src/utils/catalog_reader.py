"""Read-only access to the product catalog that ProductCatalogUpdater (pr_marketing_v2_agent)
maintains in SDK storage. The chat agent never writes to it.

Layout (under CATALOG_PREFIX, default pr_marketing_v2/knowledge):
    catalog.json             index: product overview, architecture map, one entry per feature
    architecture.json        architecture summary, components, architecture updates
    features/<slug>.json     one feature: summary, status, overview, sources, updates

The updater writes to its TENANT_ID bucket, so that is where we read unless CATALOG_BUCKET
says otherwise; both agents must point at the same bucket.
"""

import os
import re
from datetime import date, timedelta
from typing import Any

from utils import storage
from utils.text import clip

MAX_UPDATES = 15
MAX_CHANGES = 40
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
STAGE = {
    "shipped": "Shipped",
    "in_progress": "In progress",
    "planned": "Planned",
    "deprecated": "Deprecated",
}


class CatalogMissing(ValueError):
    """No catalog in storage yet; the model should fall back to docs and Jira."""


def bucket(customer_id: str | None = None) -> str:
    return os.getenv("CATALOG_BUCKET") or os.getenv("TENANT_ID") or storage.bucket_for(customer_id)


def prefix() -> str:
    return os.getenv("CATALOG_PREFIX", "pr_marketing_v2/knowledge").strip("/")


def _get(bucket_name: str, rel: str) -> Any | None:
    return storage.get_json(bucket_name, f"{prefix()}/{rel}")


def load_index(bucket_name: str) -> dict[str, Any]:
    index = _get(bucket_name, "catalog.json")
    if not index or not index.get("features"):
        raise CatalogMissing(
            "The product catalog hasn't been built yet; use the docs and Jira tools instead."
        )
    return index


def overview(bucket_name: str) -> dict[str, Any]:
    """The whole product at a glance: overview, architecture map, every feature in one line."""
    index = load_index(bucket_name)
    return {
        "product": index["product"],
        "as_of": (index.get("updated_at") or "")[:10] or None,
        "baseline": index.get("baseline"),
        "overview": index["overview"],
        "architecture": index["architecture"],
        "features": [
            {
                "slug": f["slug"],
                "name": f["name"],
                "stage": STAGE.get(f["status"], f["status"]),
                "summary": f["summary"],
                "aliases": f["aliases"],
                "last_update": f.get("last_update"),
            }
            for f in index["features"]
        ],
    }


def resolve(index: dict[str, Any], name: str) -> str:
    """A feature slug from a slug, name or alias (case-insensitive)."""
    wanted = name.strip().lower()
    for f in index["features"]:
        names = {f["slug"], f["name"].lower(), *(a.lower() for a in f["aliases"])}
        if wanted in names or wanted.replace(" ", "-") == f["slug"]:
            return f["slug"]
    known = ", ".join(f["slug"] for f in index["features"])
    raise ValueError(f"No feature {name!r} in the catalog. Known features: {known}.")


def _updates(updates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return [
        {
            "date": u["date"],
            "kind": u["kind"],
            "summary": u["summary"],
            "sources": u.get("sources", []),
            **({"component": u["component"]} if u.get("component") else {}),
        }
        for u in sorted(updates, key=lambda u: u["date"], reverse=True)[:limit]
    ]


def feature(bucket_name: str, name: str) -> dict[str, Any]:
    """One feature in full: what it is, who it's for, how it works, limits, recent updates,
    the components it runs on, and where every fact came from."""
    index = load_index(bucket_name)
    slug = resolve(index, name)
    record = _get(bucket_name, f"features/{slug}.json")
    if not record:
        raise ValueError(f"The catalog lists {slug!r} but its record is missing.")
    roles = {c["slug"]: c for c in index["architecture"]["components"]}
    edited = [m["updated_at"] for m in (record.get("field_meta") or {}).values()]
    return {
        "slug": slug,
        "name": record["name"],
        "stage": STAGE.get(record["status"], record["status"]),
        "summary": record["summary"],
        "aliases": record["aliases"],
        "overview": record["overview"],
        "components": [
            {"slug": c, "name": roles[c]["name"], "role": roles[c]["role"]}
            for c in record.get("components", [])
            if c in roles
        ],
        "recent_updates": _updates(record.get("updates", []), MAX_UPDATES),
        "sources": record.get("sources", []),
        "last_edited": max(edited, default=record.get("updated_at") or "")[:10] or None,
    }


def recent_changes(bucket_name: str, days: int, today: date | None = None) -> dict[str, Any]:
    """Every dated update across features and architecture in the last `days`, newest first."""
    index = load_index(bucket_name)
    since = ((today or date.today()) - timedelta(days=days)).isoformat()
    changes = []
    for entry in index["features"]:
        if (entry.get("last_update") or "") < since:
            continue  # the index says nothing newer is in this file
        record = _get(bucket_name, f"features/{entry['slug']}.json") or {}
        for u in record.get("updates", []):
            if u["date"] >= since:
                changes.append({**_updates([u], 1)[0], "feature": entry["slug"]})
    if (index["architecture"].get("last_update") or "") >= since:
        arch = _get(bucket_name, "architecture.json") or {}
        for u in arch.get("updates", []):
            if u["date"] >= since:
                changes.append({**_updates([u], 1)[0], "feature": "architecture"})
    changes.sort(key=lambda c: c["date"], reverse=True)
    return {
        "since": since,
        "changes": changes[:MAX_CHANGES],
        "more": len(changes) > MAX_CHANGES,
    }


def source_text(bucket_name: str, slug: str) -> dict[str, Any]:
    """A feature record as plain text, for drafting and fact-checking from the catalog."""
    if not _SLUG_RE.match(slug):
        raise ValueError(f"Bad catalog feature slug {slug!r}.")
    f = feature(bucket_name, slug)
    lines = [f"{f['name']} ({f['stage']}): {f['summary']}"]
    for field, value in f["overview"].items():
        if value:
            lines.append(f"{field.replace('_', ' ').capitalize()}: {value}")
    if f["recent_updates"]:
        lines.append("Recent updates:")
        lines += [f"- {u['date']} ({u['kind']}): {u['summary']}" for u in f["recent_updates"]]
    return {"ref": f"catalog:{slug}", "title": f["name"], "text": clip("\n".join(lines), 12000)}
