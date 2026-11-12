"""The rules code enforces on every proposed change, whatever the model said.

- validate: the operation is well formed and rests on verified claims.
- apply_status_rules: statuses follow the evidence type (docs decide what has shipped).
- tier: how much review a change needs.
  A  appends a dated update line                         -> applied automatically (live mode)
  B  rewrites what the catalog says about a feature      -> human review
  C  structural: new feature, deprecation                -> human review
"""

from datetime import datetime
from typing import Any

from utils.catalog import OVERVIEW_FIELDS, slugify

EDIT_FIELDS = ("summary", "aliases", *(f"overview.{f}" for f in OVERVIEW_FIELDS))
MAX_SUMMARY_CHARS = 300
MAX_TEXT_CHARS = 2500
REFRESH_AFTER_UPDATES = 5


def normalize(op: dict[str, Any]) -> dict[str, Any]:
    """Tidy model output: trimmed strings, lowercase slugs, a slug for new features."""
    op = {k: (v.strip() if isinstance(v, str) else v) for k, v in op.items()}
    op["target"] = (op.get("target") or "").lower()
    if op["op"] == "new_feature" and op.get("name"):
        op["target"] = slugify(op["name"])
    if op["op"] == "upsert_component" and not op["target"] and op.get("name"):
        op["target"] = slugify(op["name"])
    if op["op"] == "upsert_component":
        op["target_type"] = "component"
    op["claim_ids"] = list(dict.fromkeys(op.get("claim_ids") or []))
    return op


def validate(
    op: dict[str, Any],
    claims: dict[str, dict[str, Any]],
    feature_slugs: set[str],
    component_slugs: set[str],
) -> list[str]:
    """Problems that stop an operation; empty when it may go ahead."""
    name, target = op["op"], op.get("target", "")
    if name == "no_change":
        return []
    problems = []
    if not op["claim_ids"]:
        problems.append("cites no claims")
    unknown = [c for c in op["claim_ids"] if c not in claims]
    if unknown:
        problems.append(f"cites unverified or unknown claims {unknown}")

    on_component = op.get("target_type") == "component"
    if name in ("add_update", "edit_field", "set_status"):
        known = component_slugs if on_component else feature_slugs
        if target not in known:
            problems.append(f"unknown {'component' if on_component else 'feature'} {target!r}")
    if name in ("edit_field", "set_status") and on_component:
        problems.append(f"{name} applies to features only")

    if name == "add_update":
        if not op.get("kind"):
            problems.append("add_update needs a kind")
        if not op.get("summary"):
            problems.append("add_update needs a summary")
        elif len(op["summary"]) > MAX_SUMMARY_CHARS:
            problems.append("update summary is too long")
    elif name == "edit_field":
        if op.get("field") not in EDIT_FIELDS:
            problems.append(f"field {op.get('field')!r} can't be edited; use {EDIT_FIELDS}")
        if not op.get("text"):
            problems.append("edit_field needs the new text")
        elif len(op["text"]) > MAX_TEXT_CHARS:
            problems.append("new text is too long")
    elif name == "set_status":
        if not op.get("status"):
            problems.append("set_status needs a status")
    elif name == "upsert_component":
        if not op.get("name") or not op.get("role"):
            problems.append("upsert_component needs a name and a role")
        bad = [d for d in op.get("depends_on") or [] if d not in component_slugs | {target}]
        if bad:
            problems.append(f"depends on unknown components {bad}")
    elif name == "new_feature":
        if not op.get("name") or not op.get("summary"):
            problems.append("new_feature needs a name and a summary")
        elif target in feature_slugs:
            problems.append(f"feature {target!r} already exists; update it instead")
        bad = [c for c in op.get("components") or [] if c not in component_slugs]
        if bad:
            problems.append(f"uses unknown components {bad}")
        extra = set(op.get("overview") or {}) - set(OVERVIEW_FIELDS)
        if extra:
            problems.append(f"unknown overview fields {sorted(extra)}")
    return problems


def apply_status_rules(
    op: dict[str, Any], event: dict[str, Any], current: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """(op, None) to keep it, possibly adjusted, or (None, reason) to drop it.

    PRs show work merged, not what customers have: they never change an existing feature's
    status, and a feature first seen in a PR starts as in_progress. A docs page that adds or
    changes content may mark a feature shipped; deprecation needs docs evidence too.
    """
    refs = event.get("refs") or {}
    from_docs = event["kind"] == "docs_changed"
    page_live = from_docs and refs.get("page_status") in ("added", "changed")
    if op["op"] == "new_feature":
        return {**op, "status": "shipped" if page_live else "in_progress"}, None
    if op["op"] != "set_status":
        return op, None
    status = op.get("status")
    if not from_docs:
        return None, "a PR alone can't change a feature's status; the docs decide"
    if current and current.get("status") == status:
        return None, f"already {status}"
    if status == "shipped" and page_live:
        return op, None
    if status == "deprecated":
        return op, None
    return None, f"docs evidence can't set status {status!r}"


def tier(op: dict[str, Any]) -> str:
    name = op["op"]
    if name == "add_update":
        return "B" if op.get("kind") in ("deprecated", "removed") else "A"
    if name == "new_feature":
        return "C"
    if name == "set_status" and op.get("status") == "deprecated":
        return "C"
    return "B"


def needs_refresh(feature: dict[str, Any], threshold: int = REFRESH_AFTER_UPDATES) -> bool:
    """True when the feature has gathered `threshold` updates since its overview text last
    changed, so the overview may no longer describe it well."""
    meta = feature.get("field_meta") or {}
    edits = [m["updated_at"] for k, m in meta.items() if k.startswith("overview.")]
    since = max(edits, default=feature.get("created_at") or "")
    newer = [u for u in feature.get("updates", []) if (u.get("recorded_at") or "") > since]
    return len(newer) >= threshold


def iso_date(value: str) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
