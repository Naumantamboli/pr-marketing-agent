"""The product catalog: what Aetherion is, how it is built, and what each feature does, with a
dated trail of changes. Stored as JSON in SDK storage:

    knowledge/catalog.json                 index, small enough to load on every question
    knowledge/architecture.json            architecture summary, components and their updates
    knowledge/features/<slug>.json         one feature: overview, sources and updates
    knowledge/history/<path>/r00007.json   the record as it was before revision 8

The index repeats a few fields from the detail files (names, summaries, status, last update
date) so a reader can see everything that exists without opening them. Writes go through
`commit`: detail files first, then the index refreshed from them, so the index can only lag,
never invent.

Nothing is lost on a write. Every detail record carries a `revision`; before a record is
replaced, the stored version is copied to history under its revision. Each feature field
also remembers when it last changed and from which sources (`field_meta`), and every update
has two dates: `date` (when the change happened) and `recorded_at` (when we learned of it).

Record helpers (`new_feature`, `edit_feature`, `apply_update`, ...) are pure; the store
functions (`load_*`, `commit`, `set_*`, `upsert_*`, `add_*`) read and write.
"""

import hashlib
import json
import os
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

from utils import storage

KNOWLEDGE_PREFIX = f"{storage.PREFIX}/knowledge"
INDEX_KEY = f"{KNOWLEDGE_PREFIX}/catalog.json"
ARCHITECTURE_KEY = f"{KNOWLEDGE_PREFIX}/architecture.json"

STATUSES = ("planned", "in_progress", "shipped", "deprecated")
UPDATE_KINDS = ("added", "changed", "fixed", "deprecated", "removed")
OVERVIEW_FIELDS = ("what_it_is", "who_its_for", "how_it_works", "limitations")
# Feature fields an edit may change, as named in field_meta ("overview.<field>" for overview).
EDITABLE_FIELDS = (
    "name",
    "summary",
    "status",
    "aliases",
    "components",
    *(f"overview.{f}" for f in OVERVIEW_FIELDS),
)

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class StaleRecordError(ValueError):
    """The stored record changed since it was read; re-read and try again."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def feature_key(slug: str) -> str:
    return f"{KNOWLEDGE_PREFIX}/features/{check_slug(slug)}.json"


def history_key(key: str, revision: int) -> str:
    """Where the version of `key` at `revision` is kept once it has been replaced."""
    rel = key.removeprefix(f"{KNOWLEDGE_PREFIX}/").removesuffix(".json")
    return f"{KNOWLEDGE_PREFIX}/history/{rel}/r{revision:05d}.json"


def slugify(name: str) -> str:
    """'Slack Connector (v2)' -> 'slack-connector-v2'."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise ValueError(f"Can't make a slug from {name!r}.")
    return slug


def check_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug or ""):
        raise ValueError(f"Bad slug {slug!r}: use lowercase letters, digits and '-'.")
    return slug


def _clean_list(values: Iterable[str] | None) -> list[str]:
    """Strip, drop blanks and duplicates, keep order."""
    return list(dict.fromkeys(v.strip() for v in values or [] if v and v.strip()))


# --- updates (shared by features and architecture) ------------------------------------------


def make_update(
    summary: str,
    kind: str,
    on: str,
    sources: list[str] | None = None,
    component: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """One change entry. `on` is the date it happened (YYYY-MM-DD); `component` is set only
    for architecture updates; `sources` are PR refs, doc URLs or Jira keys; `event_id` is
    the change event it came from, which makes re-applying the same event a no-op."""
    if kind not in UPDATE_KINDS:
        raise ValueError(f"Update kind must be one of {', '.join(UPDATE_KINDS)}, not {kind!r}.")
    if not summary or not summary.strip():
        raise ValueError("An update needs a summary.")
    try:
        on = date.fromisoformat(on).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"Update date must be YYYY-MM-DD, not {on!r}.") from None
    summary = summary.strip()
    entry: dict[str, Any] = {
        "date": on,
        "kind": kind,
        "summary": summary,
        "sources": _clean_list(sources),
    }
    if component:
        entry["component"] = check_slug(component)
    if event_id:
        entry["event_id"] = event_id
    identity = f"{event_id or ''}|{component or ''}|" + ("" if event_id else f"{on}|{summary}")
    entry["id"] = "u-" + hashlib.sha1(identity.encode()).hexdigest()[:12]
    entry["recorded_at"] = _now()
    return entry


def _same_change(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two entries describe the same change when they come from the same event (or, for
    entries without one, the same primary source) about the same component. That is what
    makes a re-run of the same PR replace its entry instead of adding a second one."""
    if a.get("component") != b.get("component"):
        return False
    if a.get("event_id") and b.get("event_id"):
        return a["event_id"] == b["event_id"]
    return bool(a["sources"] and b["sources"] and a["sources"][0] == b["sources"][0])


def apply_update(record: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """The record with `entry` added to its updates (oldest first)."""
    kept = [u for u in record.get("updates", []) if not _same_change(u, entry)]
    updates = sorted([*kept, entry], key=lambda u: u["date"])
    return {**record, "updates": updates, "updated_at": _now()}


def has_update_from(record: dict[str, Any], event_id: str, component: str | None = None) -> bool:
    return any(
        u.get("event_id") == event_id and u.get("component") == component
        for u in record.get("updates", [])
    )


def _last_update(record: dict[str, Any]) -> str | None:
    return record["updates"][-1]["date"] if record.get("updates") else None


# --- records ---------------------------------------------------------------------------------


def empty_index(product: str | None = None) -> dict[str, Any]:
    return {
        "product": product or os.getenv("PRODUCT_ID", "aetherion"),
        "updated_at": None,
        "revision": 0,
        "baseline": None,  # {"as_of", "source"} of the seed the catalog started from
        "overview": "",
        "architecture": {"summary": "", "components": [], "last_update": None},
        "features": [],
    }


def empty_architecture() -> dict[str, Any]:
    return {"summary": "", "components": [], "updates": [], "updated_at": None, "revision": 0}


def new_feature(name: str, slug: str | None = None) -> dict[str, Any]:
    if not name or not name.strip():
        raise ValueError("A feature needs a name.")
    now = _now()
    return {
        "slug": check_slug(slug or slugify(name)),
        "name": name.strip(),
        "summary": "",
        "status": "shipped",
        "aliases": [],
        "components": [],
        "overview": dict.fromkeys(OVERVIEW_FIELDS, ""),
        "sources": [],
        "updates": [],
        "field_meta": {},
        "revision": 0,
        "created_at": now,
        "updated_at": now,
    }


def get_field(feature: dict[str, Any], field: str) -> Any:
    """A feature field by its field_meta name, e.g. 'summary' or 'overview.limitations'."""
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"Unknown feature field {field!r}; use {', '.join(EDITABLE_FIELDS)}.")
    if field.startswith("overview."):
        return feature["overview"].get(field.split(".", 1)[1], "")
    return feature[field]


def edit_feature(
    feature: dict[str, Any],
    name: str | None = None,
    summary: str | None = None,
    status: str | None = None,
    aliases: list[str] | None = None,
    components: list[str] | None = None,
    overview: dict[str, str] | None = None,
    sources: list[str] | None = None,
    provenance: list[str] | None = None,
) -> dict[str, Any]:
    """The feature with the given fields changed; None leaves a field alone. `overview` may
    hold only some of OVERVIEW_FIELDS; `sources` are added to the existing ones.
    `provenance` (the sources behind this edit) is recorded in field_meta for every field
    whose value actually changes."""
    out = {**feature, "overview": dict(feature["overview"])}
    changes: dict[str, Any] = {}
    if name is not None:
        if not name.strip():
            raise ValueError("A feature needs a name.")
        changes["name"] = name.strip()
    if summary is not None:
        changes["summary"] = summary.strip()
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"Status must be one of {', '.join(STATUSES)}, not {status!r}.")
        changes["status"] = status
    if aliases is not None:
        changes["aliases"] = _clean_list(aliases)
    if components is not None:
        changes["components"] = [check_slug(c) for c in _clean_list(components)]
    if overview is not None:
        unknown = set(overview) - set(OVERVIEW_FIELDS)
        if unknown:
            raise ValueError(
                f"Unknown overview field(s) {sorted(unknown)}; use {', '.join(OVERVIEW_FIELDS)}."
            )
        for k, v in overview.items():
            changes[f"overview.{k}"] = (v or "").strip()

    now = _now()
    meta = dict(feature.get("field_meta") or {})
    for field, value in changes.items():
        if get_field(out, field) == value:
            continue
        if field.startswith("overview."):
            out["overview"][field.split(".", 1)[1]] = value
        else:
            out[field] = value
        meta[field] = {"updated_at": now, "sources": _clean_list(provenance)}
    out["field_meta"] = meta
    if sources is not None:
        out["sources"] = _clean_list([*feature["sources"], *sources])
    out["updated_at"] = now
    return out


def upsert_component_record(
    architecture: dict[str, Any],
    name: str,
    role: str | None = None,
    depends_on: list[str] | None = None,
    slug: str | None = None,
) -> dict[str, Any]:
    """The architecture with a component added, or its name/role/dependencies changed."""
    slug = check_slug(slug or slugify(name))
    components = list(architecture["components"])
    at = next((i for i, c in enumerate(components) if c["slug"] == slug), None)
    current = components[at] if at is not None else {"slug": slug, "role": "", "depends_on": []}
    component = {**current, "name": name.strip()}
    if role is not None:
        component["role"] = role.strip()
    if depends_on is not None:
        component["depends_on"] = [check_slug(d) for d in _clean_list(depends_on) if d != slug]
    if at is None:
        components.append(component)
    else:
        components[at] = component
    return {**architecture, "components": components, "updated_at": _now()}


def find_component(architecture: dict[str, Any], slug: str) -> dict[str, Any] | None:
    return next((c for c in architecture["components"] if c["slug"] == slug), None)


def feature_entry(feature: dict[str, Any]) -> dict[str, Any]:
    """What the index keeps about a feature."""
    return {
        "slug": feature["slug"],
        "name": feature["name"],
        "summary": feature["summary"],
        "status": feature["status"],
        "aliases": feature["aliases"],
        "last_update": _last_update(feature),
    }


def with_feature(index: dict[str, Any], feature: dict[str, Any]) -> dict[str, Any]:
    """The index with this feature's entry added or refreshed, in place if it was there."""
    entry = feature_entry(feature)
    features = list(index["features"])
    at = next((i for i, f in enumerate(features) if f["slug"] == entry["slug"]), None)
    if at is None:
        features.append(entry)
    else:
        features[at] = entry
    return {**index, "features": features}


def with_architecture(index: dict[str, Any], architecture: dict[str, Any]) -> dict[str, Any]:
    """The index with the architecture summary and component map refreshed."""
    return {
        **index,
        "architecture": {
            "summary": architecture["summary"],
            "components": [
                {"slug": c["slug"], "name": c["name"], "role": c["role"]}
                for c in architecture["components"]
            ],
            "last_update": _last_update(architecture),
        },
    }


# --- storage ---------------------------------------------------------------------------------


def _read(key: str) -> Any | None:
    raw = storage.get(key)
    return json.loads(raw) if raw else None


def _write(key: str, value: dict[str, Any]) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2).encode()
    storage.put(key, data, "application/json")


def _commit_record(key: str, record: dict[str, Any]) -> dict[str, Any]:
    """Write a detail record, keeping the version it replaces. The record's `revision` must
    be the stored one (0 for a new record), otherwise someone else wrote in between."""
    current = _read(key)
    stored = (current or {}).get("revision", 0) if current else 0
    expected = record.get("revision", 0)
    if current is not None and expected != stored:
        raise StaleRecordError(
            f"{key} is at revision {stored}, but this change was made from revision {expected}."
        )
    if current is None and expected:
        raise StaleRecordError(f"{key} no longer exists (expected revision {expected}).")
    if current is not None:
        _write(history_key(key, stored), current)
    record = {**record, "revision": stored + 1 if current is not None else 1}
    _write(key, record)
    return record


def load_index() -> dict[str, Any]:
    return _read(INDEX_KEY) or empty_index()


def load_architecture() -> dict[str, Any]:
    return _read(ARCHITECTURE_KEY) or empty_architecture()


def load_feature(slug: str) -> dict[str, Any] | None:
    return _read(feature_key(slug))


def load_features(slugs: Iterable[str]) -> list[dict[str, Any]]:
    return [f for s in slugs if (f := load_feature(s))]


def load_history(key: str, revision: int) -> dict[str, Any] | None:
    """The record at `key` as it was at `revision` (before it was replaced)."""
    return _read(history_key(key, revision))


def commit(
    features: Iterable[dict[str, Any]] = (),
    architecture: dict[str, Any] | None = None,
    index_changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save detail records (with revision checks and history), then the index once.

    `index_changes` sets index-only fields such as `overview` or `baseline`; when the
    overview changes, the previous index is kept in history too. Returns the new index.
    """
    saved_features = [_commit_record(feature_key(f["slug"]), f) for f in features]
    saved_arch = _commit_record(ARCHITECTURE_KEY, architecture) if architecture else None

    index = load_index()
    changes = index_changes or {}
    if "overview" in changes and changes["overview"] != index.get("overview"):
        _write(history_key(INDEX_KEY, index.get("revision", 0)), index)
    index = {**index, **changes}
    if saved_arch:
        index = with_architecture(index, saved_arch)
    for feature in saved_features:
        index = with_feature(index, feature)
    index = {**index, "updated_at": _now(), "revision": index.get("revision", 0) + 1}
    _write(INDEX_KEY, index)
    return index


def _require_feature(slug: str) -> dict[str, Any]:
    feature = load_feature(slug)
    if not feature:
        raise ValueError(f"No feature {slug!r} in the catalog; add it with upsert_feature.")
    return feature


def set_overview(text: str) -> dict[str, Any]:
    """Replace the product overview (it summarises everything else, so it has no updates)."""
    return commit(index_changes={"overview": text.strip()})


def set_architecture_summary(text: str) -> dict[str, Any]:
    arch = load_architecture()
    commit(architecture={**arch, "summary": text.strip(), "updated_at": _now()})
    return load_architecture()


def upsert_component(
    name: str,
    role: str | None = None,
    depends_on: list[str] | None = None,
    slug: str | None = None,
) -> dict[str, Any]:
    commit(architecture=upsert_component_record(load_architecture(), name, role, depends_on, slug))
    return load_architecture()


def add_architecture_update(
    summary: str,
    kind: str,
    on: str,
    component: str,
    sources: list[str] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    arch = load_architecture()
    if not find_component(arch, component):
        raise ValueError(f"No component {component!r}; add it with upsert_component first.")
    entry = make_update(summary, kind, on, sources, component=component, event_id=event_id)
    commit(architecture=apply_update(arch, entry))
    return load_architecture()


def upsert_feature(name: str, slug: str | None = None, **fields: Any) -> dict[str, Any]:
    """Add a feature or change it; `fields` are edit_feature's keyword arguments. The slug
    is fixed once the feature exists, so renaming keeps the same file."""
    slug = check_slug(slug or slugify(name))
    feature = load_feature(slug) or new_feature(name, slug)
    commit(features=[edit_feature(feature, name=name, **fields)])
    return load_feature(slug)


def add_feature_update(
    slug: str,
    summary: str,
    kind: str,
    on: str,
    sources: list[str] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    feature = _require_feature(slug)
    entry = make_update(summary, kind, on, sources, event_id=event_id)
    if sources:
        feature = edit_feature(feature, sources=sources)
    commit(features=[apply_update(feature, entry)])
    return load_feature(slug)


def restore_feature(slug: str, revision: int) -> dict[str, Any]:
    """Roll a feature back to an earlier revision (saved as a new revision, so the rollback
    itself is reversible)."""
    current = _require_feature(slug)
    old = load_history(feature_key(slug), revision)
    if not old:
        raise ValueError(f"No revision {revision} of feature {slug!r} in history.")
    commit(features=[{**old, "revision": current["revision"], "updated_at": _now()}])
    return load_feature(slug)


def check_seed(seed: dict[str, Any]) -> list[str]:
    """What's wrong with a baseline seed (see seed/catalog_seed.json); empty when it loads."""
    problems = []
    components = [c.get("slug") for c in seed.get("architecture", {}).get("components", [])]
    known = set(components)
    if len(known) != len(components):
        problems.append("duplicate component slugs")
    for c in seed.get("architecture", {}).get("components", []):
        for dep in c.get("depends_on", []):
            if dep not in known:
                problems.append(f"component {c['slug']!r} depends on unknown {dep!r}")
    slugs = [f.get("slug") for f in seed.get("features", [])]
    if len(set(slugs)) != len(slugs):
        problems.append("duplicate feature slugs")
    for f in seed.get("features", []):
        for c in f.get("components", []):
            if c not in known:
                problems.append(f"feature {f['slug']!r} uses unknown component {c!r}")
        if not f.get("sources"):
            problems.append(f"feature {f['slug']!r} has no sources")
    return problems


def apply_seed(seed: dict[str, Any]) -> dict[str, Any]:
    """Load a baseline seed into storage and return the new index. Re-running it is safe:
    texts are replaced, sources are merged, and existing updates are kept."""
    problems = check_seed(seed)
    if problems:
        raise ValueError("Seed not loaded: " + "; ".join(problems))
    provenance = [seed["source"]] if seed.get("source") else None

    arch = {**load_architecture(), "summary": seed["architecture"]["summary"].strip()}
    for c in seed["architecture"]["components"]:
        arch = upsert_component_record(arch, c["name"], c["role"], c.get("depends_on"), c["slug"])

    fields = ("summary", "status", "aliases", "components", "overview", "sources")
    features = []
    for f in seed["features"]:
        feature = load_feature(f["slug"]) or new_feature(f["name"], f["slug"])
        changes = {k: f[k] for k in fields if k in f}
        why = f.get("sources") or provenance
        features.append(edit_feature(feature, name=f["name"], provenance=why, **changes))

    return commit(
        features=features,
        architecture=arch,
        index_changes={
            "product": seed.get("product") or empty_index()["product"],
            "overview": seed["overview"].strip(),
            "baseline": {"as_of": seed.get("as_of"), "source": seed.get("source")},
        },
    )


def remove_feature(slug: str) -> dict[str, Any]:
    """Drop a feature from the index. Its file stays in storage as a record of what it was;
    for a feature that was retired, prefer status='deprecated' so readers still learn of it."""
    index = load_index()
    if not any(f["slug"] == slug for f in index["features"]):
        raise ValueError(f"No feature {slug!r} in the catalog.")
    kept = [f for f in index["features"] if f["slug"] != slug]
    index = {**index, "features": kept, "updated_at": _now(), "revision": index["revision"] + 1}
    _write(INDEX_KEY, index)
    return index


# --- reading ---------------------------------------------------------------------------------


def find_feature(index: dict[str, Any], text: str) -> dict[str, Any] | None:
    """The index entry whose slug, name or alias matches `text` (case-insensitive)."""
    wanted = text.strip().lower()
    for f in index["features"]:
        if wanted in (f["slug"], f["name"].lower(), *(a.lower() for a in f["aliases"])):
            return f
    return None


def _updates_md(updates: list[dict[str, Any]], limit: int) -> list[str]:
    lines = []
    for u in list(reversed(updates))[:limit]:
        where = f" [{u['component']}]" if u.get("component") else ""
        refs = f" ({', '.join(u['sources'])})" if u["sources"] else ""
        lines.append(f"- **{u['date']}** {u['kind']}{where}: {u['summary']}{refs}")
    return lines or ["_None yet._"]


def to_markdown(
    index: dict[str, Any],
    architecture: dict[str, Any] | None = None,
    features: list[dict[str, Any]] | None = None,
    recent_updates: int = 5,
) -> str:
    """The catalog as a readable page: Overview, Architecture, then each feature with its
    overview and latest updates. Without the detail records, the index entries are used."""
    by_slug = {f["slug"]: f for f in features or []}
    arch = architecture or {**index["architecture"], "updates": []}
    lines = [f"# {index['product'].title()}", "", "## Overview", ""]
    lines += [index["overview"] or "_Not written yet._", "", "## Architecture", ""]
    lines += [arch["summary"] or "_Not written yet._"]
    if arch["components"]:
        lines += ["", "### Components", ""]
        for c in arch["components"]:
            deps = c.get("depends_on") or []
            uses = f" Uses: {', '.join(deps)}." if deps else ""
            lines.append(f"- **{c['name']}** (`{c['slug']}`): {c['role'] or '—'}{uses}")
    if architecture:
        lines += ["", "### Updates", "", *_updates_md(architecture["updates"], recent_updates)]
    lines += ["", "## Features"]
    for entry in index["features"]:
        f = by_slug.get(entry["slug"])
        lines += ["", f"### {entry['name']} ({entry['status'].replace('_', ' ')})", ""]
        lines.append(entry["summary"] or "_No summary yet._")
        if not f:
            continue
        lines += ["", "#### Overview", ""]
        for field in OVERVIEW_FIELDS:
            if f["overview"].get(field):
                lines.append(f"**{field.replace('_', ' ').capitalize()}:** {f['overview'][field]}")
                lines.append("")
        if not any(f["overview"].values()):
            lines += ["_Not written yet._", ""]
        lines += ["#### Updates", "", *_updates_md(f["updates"], recent_updates)]
    return "\n".join(lines) + "\n"


def render() -> str:
    """Load everything and render it with `to_markdown`."""
    index = load_index()
    features = load_features(e["slug"] for e in index["features"])
    return to_markdown(index, load_architecture(), features)
