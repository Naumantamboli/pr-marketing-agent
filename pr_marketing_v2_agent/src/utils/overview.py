"""The Product Overview: one compact, approved map of the whole product, in SDK storage.

Every version is a new, never-edited file (overview/v0007.json). overview/head.json says
which version the chat reads ("published") and which one waits for an SME ("pending").
Publishing or rolling back only moves the pointer.
"""

import json
from typing import Any

from utils import storage
from utils.catalog import KNOWLEDGE_PREFIX

HEAD_KEY = f"{KNOWLEDGE_PREFIX}/overview/head.json"

# Plan §4.1. Lists hold dicts; each capability/workflow/relationship links to catalog slugs.
SECTIONS = (
    "identity",
    "capabilities",
    "architecture",
    "workflows",
    "relationships",
    "constraints",
    "coverage",
)
COVERAGE_STATES = ("covered", "partial", "missing", "conflicting")


def version_key(version: int) -> str:
    return f"{KNOWLEDGE_PREFIX}/overview/v{version:04d}.json"


def _read(key: str) -> dict[str, Any] | None:
    raw = storage.get(key)
    return json.loads(raw) if raw else None


def _write(key: str, value: dict[str, Any]) -> None:
    storage.put(key, json.dumps(value, ensure_ascii=False, indent=1).encode(), "application/json")


def head() -> dict[str, Any]:
    return _read(HEAD_KEY) or {"latest": 0, "published": None, "pending": None, "history": []}


def get_version(version: int) -> dict[str, Any]:
    found = _read(version_key(version))
    if found is None:
        raise ValueError(f"Overview version {version} doesn't exist.")
    return found


def get_published() -> dict[str, Any] | None:
    published = head()["published"]
    return get_version(published) if published else None


def linked_features(overview: dict[str, Any]) -> set[str]:
    """Every feature slug the overview points at."""
    slugs: set[str] = set()
    for item in overview.get("capabilities") or []:
        slugs.update(filter(None, [item.get("feature")]))
    for item in overview.get("workflows") or []:
        slugs.update(item.get("features") or [])
    for item in overview.get("relationships") or []:
        slugs.update(filter(None, [item.get("from"), item.get("to")]))
    return slugs


def problems(overview: dict[str, Any], catalog: dict[str, Any]) -> list[str]:
    """What's wrong with a draft; empty when it can be saved."""
    found = [f"missing section {s!r}" for s in SECTIONS if s not in overview]
    identity = overview.get("identity") or {}
    if not identity.get("what_it_is"):
        found.append("identity.what_it_is is empty")
    for name in ("capabilities", "architecture", "workflows", "relationships", "constraints"):
        if name in overview and not isinstance(overview[name], list):
            found.append(f"{name} must be a list")
    published = {s for s, e in catalog["features"].items() if e["state"] == "published"}
    found += [
        f"links to unknown feature {s!r}" for s in sorted(linked_features(overview) - published)
    ]
    bad = {k: v for k, v in (overview.get("coverage") or {}).items() if v not in COVERAGE_STATES}
    found += [f"coverage[{k!r}] = {v!r} (use {', '.join(COVERAGE_STATES)})" for k, v in bad.items()]
    return found


def save_draft(overview: dict[str, Any], catalog: dict[str, Any], *, at: str, reason: str) -> int:
    """Store a new version as pending (it replaces any earlier pending draft) and return it."""
    errors = problems(overview, catalog)
    if errors:
        raise ValueError("Overview draft isn't valid: " + "; ".join(errors))
    state = head()
    version = state["latest"] + 1
    _write(
        version_key(version), {**overview, "version": version, "created_at": at, "reason": reason}
    )
    state.update(latest=version, pending=version)
    state["history"].append({"at": at, "event": "draft", "version": version, "reason": reason})
    _write(HEAD_KEY, state)
    return version


def publish(version: int, *, at: str, approved_by: str) -> dict[str, Any]:
    """Make `version` the one the chat reads. Also used to roll back to an older version."""
    get_version(version)  # must exist
    state = head()
    previous = state["published"]
    state["published"] = version
    if state["pending"] == version:
        state["pending"] = None
    state["history"].append(
        {"at": at, "event": "publish", "version": version, "previous": previous, "by": approved_by}
    )
    _write(HEAD_KEY, state)
    return state


def discard_pending(*, at: str, by: str, reason: str) -> dict[str, Any]:
    """Reject the pending draft. Its file stays for the record; nothing points at it."""
    state = head()
    if state["pending"]:
        state["history"].append(
            {"at": at, "event": "discard", "version": state["pending"], "by": by, "reason": reason}
        )
        state["pending"] = None
        _write(HEAD_KEY, state)
    return state


def sections_for(overview: dict[str, Any], slug: str) -> list[str]:
    """Overview sections that mention a feature: what to rebuild when it changes."""
    hits = []
    if any(c.get("feature") == slug for c in overview.get("capabilities") or []):
        hits.append("capabilities")
    if any(slug in (w.get("features") or []) for w in overview.get("workflows") or []):
        hits.append("workflows")
    if any(slug in (r.get("from"), r.get("to")) for r in overview.get("relationships") or []):
        hits.append("relationships")
    return hits
