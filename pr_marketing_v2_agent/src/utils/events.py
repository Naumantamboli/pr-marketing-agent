"""Change events: one normalized shape for everything that can change the catalog.

    {
      "event_id": "pr:org/repo#412" | "docs:canvas@3f2a9c1e0b",   # stable idempotency key
      "kind": "pr_merged" | "docs_changed",
      "occurred_at": "YYYY-MM-DD",
      "title": str, "url": str,
      "refs": {...},                                              # jira keys, doc link, ...
      "evidence": [{"id": "e1", "source": "pr", "url", "text"}, ...]
    }

Evidence is the only text later steps may quote. It is redacted and clipped here, and
stored once (knowledge/events/<id>.json); workflows pass the storage key, not the text.
"""

import re
from typing import Any

from utils import storage
from utils.catalog import KNOWLEDGE_PREFIX
from utils.text import clip, redact

EVIDENCE_CHARS = 8000
_JIRA_KEY_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9_]+-\d+)(?![A-Za-z0-9])")
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def pr_event_id(repo: str, number: int) -> str:
    return f"pr:{repo}#{number}"


def docs_event_id(slug: str, content_hash: str) -> str:
    return f"docs:{slug}@{content_hash[:10]}"


def event_key(event_id: str) -> str:
    return f"{KNOWLEDGE_PREFIX}/events/{_SAFE_RE.sub('_', event_id)}.json"


def jira_keys(*texts: str, projects: list[str] | None = None) -> list[str]:
    """Jira keys mentioned in the texts, in order, deduplicated; limited to `projects` when
    given (branch names like feat/AP-12-x and titles like "[AP-12] ..." both match)."""
    keys = [k for t in texts for k in _JIRA_KEY_RE.findall((t or "").upper())]
    if projects:
        allowed = {p.upper() for p in projects}
        keys = [k for k in keys if k.rsplit("-", 1)[0] in allowed]
    return list(dict.fromkeys(keys))


def evidence(eid: str, source: str, url: str | None, text: str) -> dict[str, Any]:
    return {"id": eid, "source": source, "url": url, "text": clip(redact(text), EVIDENCE_CHARS)}


def _pr_text(pr: dict[str, Any]) -> str:
    parts = [f"Title: {pr['title']}"]
    if pr.get("body"):
        parts.append(f"Description:\n{pr['body']}")
    if pr.get("commits"):
        parts.append("Commits:\n" + "\n".join(f"- {c}" for c in pr["commits"]))
    if pr.get("labels"):
        parts.append("Labels: " + ", ".join(pr["labels"]))
    return "\n\n".join(parts)


def _jira_text(issue: dict[str, Any]) -> str:
    head = (
        f"{issue['key']} ({issue.get('type')}, status: {issue.get('status')}): {issue['summary']}"
    )
    lines = [head]
    if issue.get("epic"):
        lines.append(f"Epic: {issue['epic']}")
    if issue.get("fix_versions"):
        lines.append("Fix versions: " + ", ".join(issue["fix_versions"]))
    if issue.get("description"):
        lines.append("")
        lines.append(issue["description"])
    return "\n".join(lines)


def pr_event(pr: dict[str, Any], issues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A merged PR (from GitHubClient.fetch_pr) plus its Jira issues as one event."""
    items = [evidence("e1", "pr", pr["url"], _pr_text(pr))]
    for i, issue in enumerate(issues or [], start=2):
        url = issue.get("url")
        items.append(evidence(f"e{i}", f"jira:{issue['key']}", url, _jira_text(issue)))
    doc = pr.get("feature_doc") or {}
    return {
        "event_id": pr_event_id(pr["repo"], pr["number"]),
        "kind": "pr_merged",
        "occurred_at": (pr.get("merged_at") or "")[:10],
        "title": pr["title"],
        "url": pr["url"],
        "refs": {
            "jira": [i["key"] for i in issues or []],
            "feature_doc": doc.get("url") if doc.get("status") == "ok" else None,
            "type_check": pr.get("type_check"),
        },
        "evidence": items,
    }


def docs_event(change: dict[str, Any], detected_on: str) -> dict[str, Any]:
    """A changed docs page (from docs_site.diff_snapshots). Evidence is only what changed,
    so claims can't be drawn from text that was already true before."""
    items = []
    if change["added"]:
        text = f"Added or changed on the '{change['title']}' docs page:\n\n"
        items.append(
            evidence(
                "e1", f"docs:{change['slug']}", change["url"], text + "\n\n".join(change["added"])
            )
        )
    if change["removed"]:
        text = f"Removed from the '{change['title']}' docs page:\n\n"
        eid = f"e{len(items) + 1}"
        items.append(
            evidence(
                eid, f"docs:{change['slug']}", change["url"], text + "\n\n".join(change["removed"])
            )
        )
    return {
        "event_id": docs_event_id(change["slug"], change["hash"] or "removed"),
        "kind": "docs_changed",
        "occurred_at": detected_on,
        "title": f"Docs: {change['title']} ({change['status']})",
        "url": change["url"],
        "refs": {"docs_page": change["url"], "page_status": change["status"]},
        "evidence": items,
    }


def save_event(event: dict[str, Any]) -> str:
    key = event_key(event["event_id"])
    storage.put_json(key, event)
    return key


def load_event(key: str) -> dict[str, Any]:
    event = storage.get_json(key)
    if not event:
        raise ValueError(f"No event stored at {key}.")
    return event


def evidence_by_id(event: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in event["evidence"]}
