"""Proposals: checked catalog changes waiting to be applied, and the code that applies them.

A proposal carries the operation, the claims and quotes behind it, its risk tier, a
before/after preview for reviewers, and `expected`: the value the target had when the
proposal was made. Applying re-reads the target and refuses (status "stale") if that value
has changed, so a reviewer never approves a change against text that is no longer there.

Storage:
    knowledge/proposals/pending.json   proposals waiting for review (tier B/C, live mode)
    knowledge/runs/<run_id>.json       audit: every proposal a run made and what happened
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from utils import catalog, storage
from utils.catalog import KNOWLEDGE_PREFIX
from utils.prompts import op_text

PENDING_KEY = f"{KNOWLEDGE_PREFIX}/proposals/pending.json"
MAX_REVIEW_ROUNDS = 3  # unanswered this many times -> expired
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def run_key(run_id: str) -> str:
    return f"{KNOWLEDGE_PREFIX}/runs/{_SAFE_RE.sub('_', run_id)}.json"


def report_key(run_id: str) -> str:
    return f"{KNOWLEDGE_PREFIX}/runs/{_SAFE_RE.sub('_', run_id)}.md"


def proposal_id(event_id: str, op: dict[str, Any]) -> str:
    core = {k: op.get(k) for k in ("op", "target_type", "target", "field", "status", "kind")}
    raw = event_id + json.dumps(core, sort_keys=True)
    return "p-" + hashlib.sha1(raw.encode()).hexdigest()[:12]


def _current(op: dict[str, Any], feature: dict | None, arch: dict) -> Any:
    """The value an operation replaces (what `expected` records)."""
    name = op["op"]
    if name == "edit_field" and feature:
        return catalog.get_field(feature, op["field"])
    if name == "set_status" and feature:
        return feature["status"]
    if name == "upsert_component":
        c = catalog.find_component(arch, op["target"])
        return {"role": c["role"], "depends_on": c.get("depends_on", [])} if c else None
    return None


def _as_text(value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, list):
        return ", ".join(value) or "(empty)"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value) or "(empty)"


def preview(op: dict[str, Any], feature: dict | None, arch: dict) -> dict[str, str]:
    name = op["op"]
    before = _current(op, feature, arch)
    if name == "edit_field":
        after: Any = op["text"]
        if op["field"] == "aliases":
            after = [a.strip() for a in op["text"].split(",") if a.strip()]
    elif name == "set_status":
        after = op["status"]
    elif name == "upsert_component":
        after = {"role": op["role"], "depends_on": op.get("depends_on") or []}
    elif name == "add_update":
        before, after = None, f"[{op['kind']}] {op['summary']}"
    else:
        after = op_text(op)
    return {"before": _as_text(before), "after": _as_text(after)}


def make_proposal(
    event: dict[str, Any],
    op: dict[str, Any],
    claims: dict[str, dict[str, Any]],
    tier: str,
    feature: dict | None,
    arch: dict,
    note: str = "",
) -> dict[str, Any]:
    return {
        "id": proposal_id(event["event_id"], op),
        "event_id": event["event_id"],
        "event_title": event["title"],
        "event_url": event["url"],
        "occurred_at": event["occurred_at"],
        "sources": [event["url"], *(event.get("refs") or {}).get("jira", [])],
        "op": op,
        "tier": tier,
        "claims": [claims[c] for c in op["claim_ids"] if c in claims],
        "note": note,
        "preview": preview(op, feature, arch),
        "expected": _current(op, feature, arch),
        "status": "pending",
        "review_rounds": 0,
        "created_at": _now(),
    }


def _apply_one(
    p: dict[str, Any], features: dict[str, dict[str, Any]], arch: dict[str, Any]
) -> dict[str, Any]:
    """Apply one proposal to the in-memory records; returns the new architecture."""
    op, slug = p["op"], p["op"]["target"]
    feature = features.get(slug) if op.get("target_type") != "component" else None
    if op["op"] != "new_feature" and op.get("target_type") != "component" and not feature:
        raise catalog.StaleRecordError(f"feature {slug!r} no longer exists")
    if p["expected"] != _current(op, feature, arch):
        raise catalog.StaleRecordError("the catalog changed since this was proposed")
    sources, why = p["sources"], [p["event_url"]]

    if op["op"] == "add_update":
        on_component = op.get("target_type") == "component"
        entry = catalog.make_update(
            op["summary"],
            op["kind"],
            p["occurred_at"],
            sources,
            component=slug if on_component else None,
            event_id=p["event_id"],
        )
        if on_component:
            return catalog.apply_update(arch, entry)
        features[slug] = catalog.apply_update(catalog.edit_feature(feature, sources=sources), entry)
    elif op["op"] == "edit_field":
        field = op["field"]
        if field == "aliases":
            change = {"aliases": [a.strip() for a in op["text"].split(",") if a.strip()]}
        elif field.startswith("overview."):
            change = {"overview": {field.split(".", 1)[1]: op["text"]}}
        else:
            change = {field: op["text"]}
        features[slug] = catalog.edit_feature(feature, sources=sources, provenance=why, **change)
    elif op["op"] == "set_status":
        features[slug] = catalog.edit_feature(
            feature, status=op["status"], sources=sources, provenance=why
        )
    elif op["op"] == "upsert_component":
        return catalog.upsert_component_record(
            arch, op["name"], op["role"], op.get("depends_on"), slug
        )
    elif op["op"] == "new_feature":
        if slug in features or catalog.load_feature(slug):
            raise catalog.StaleRecordError(f"feature {slug!r} already exists")
        features[slug] = catalog.edit_feature(
            catalog.new_feature(op["name"], slug),
            summary=op["summary"],
            status=op.get("status") or "in_progress",
            aliases=op.get("aliases") or [],
            components=op.get("components") or [],
            overview={k: v for k, v in (op.get("overview") or {}).items() if v},
            sources=sources,
            provenance=why,
        )
    return arch


def apply(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply proposals in order and save everything in one commit. Returns the proposals
    with status 'applied', 'stale' (target changed; needs a fresh look) or 'failed'."""
    slugs = {p["op"]["target"] for p in proposals if p["op"].get("target_type") != "component"}
    features = {f["slug"]: f for f in catalog.load_features(slugs)}
    originals = {s: f["revision"] for s, f in features.items()}
    arch = catalog.load_architecture()
    arch_revision = arch.get("revision", 0)
    arch_changed = False
    touched: list[str] = []

    done = []
    for p in proposals:
        try:
            new_arch = _apply_one(p, features, arch)
            arch_changed = arch_changed or new_arch is not arch
            arch = new_arch
            if p["op"].get("target_type") != "component":
                touched.append(p["op"]["target"])
            done.append({**p, "status": "applied", "applied_at": _now()})
        except catalog.StaleRecordError as err:
            done.append({**p, "status": "stale", "error": str(err)})
        except ValueError as err:
            done.append({**p, "status": "failed", "error": str(err)})

    if touched or arch_changed:
        catalog.commit(
            # Each record keeps the revision it was read at, so commit's check catches any
            # write that happened in between.
            features=[
                {**features[s], "revision": originals.get(s, 0)} for s in dict.fromkeys(touched)
            ],
            architecture={**arch, "revision": arch_revision} if arch_changed else None,
        )
    return done


# --- pending queue and run audit -------------------------------------------------------------


def load_pending() -> list[dict[str, Any]]:
    return storage.get_json(PENDING_KEY) or []


def save_pending(items: list[dict[str, Any]]) -> None:
    storage.put_json(PENDING_KEY, items)


def queue(new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add proposals to the pending queue (a proposal already queued keeps its place)."""
    pending = load_pending()
    have = {p["id"] for p in pending}
    pending += [p for p in new if p["id"] not in have]
    save_pending(pending)
    return pending


def take(ids: list[str]) -> list[dict[str, Any]]:
    """Remove and return the queued proposals with these ids, in queue order."""
    wanted = set(ids)
    pending = load_pending()
    save_pending([p for p in pending if p["id"] not in wanted])
    return [p for p in pending if p["id"] in wanted]


def append_audit(run_id: str, section: str, items: list[dict[str, Any]]) -> None:
    key = run_key(run_id)
    audit = storage.get_json(key) or {"run_id": run_id, "created_at": _now()}
    audit.setdefault(section, []).extend(items)
    audit["updated_at"] = _now()
    storage.put_json(key, audit)
