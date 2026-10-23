"""Read-only Jira Cloud client (REST v3, API-token basic auth).

The model never writes JQL: it passes plain filters and the query is built here, always scoped
to JIRA_PROJECTS, so a prompt can't widen what the agent reads.
"""

import base64
import os
import re
from typing import Any

import httpx

from utils.text import clip, redact

MAX_ISSUES = 30
DESCRIPTION_CHARS = 6000
_FIELDS = [
    "summary",
    "status",
    "issuetype",
    "labels",
    "fixVersions",
    "resolutiondate",
    "updated",
    "parent",
]
_KEY_RE = re.compile(r"^(?P<project>[A-Z][A-Z0-9_]+)-\d+$")
# Characters with meaning in Jira text search; dropped from what the model searches for.
_TEXT_SPECIALS_RE = re.compile(r'[+\-&|!(){}\[\]^~*?\\:"\']')
_STATUS = {
    "done": 'statusCategory = "Done"',
    "in_progress": 'statusCategory = "In Progress"',
    "planned": 'statusCategory = "To Do"',
}
# What marketing should call each Jira status category.
STAGE = {"done": "done", "indeterminate": "in progress", "new": "planned"}


def allowed_projects() -> list[str]:
    raw = os.getenv("JIRA_PROJECTS", "")
    return [p.strip().upper() for p in raw.split(",") if p.strip()]


def build_jql(
    projects: list[str], text: str | None = None, days: int | None = None, status: str = "any"
) -> str:
    if not projects:
        raise ValueError("JIRA_PROJECTS is not set, so I don't know which Jira projects to read.")
    clauses = [f"project in ({', '.join(projects)})", "issuetype not in subTaskIssueTypes()"]
    if status not in ("any", *_STATUS):
        raise ValueError(f"status must be one of any, {', '.join(_STATUS)}")
    if status != "any":
        clauses.append(_STATUS[status])
    if text:
        words = " ".join(_TEXT_SPECIALS_RE.sub(" ", text).split())
        if words:
            clauses.append(f'text ~ "{words}"')
    if days:
        field = "resolved" if status == "done" else "updated"
        clauses.append(f"{field} >= -{int(days)}d")
    order = "resolved DESC" if status == "done" else "updated DESC"
    return " AND ".join(clauses) + f" ORDER BY {order}"


def adf_to_text(node: Any) -> str:
    """Atlassian Document Format -> plain text (paragraphs, headings, lists, mentions)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji"):
        return attrs.get("text") or attrs.get("shortName") or ""
    if kind == "inlineCard":
        return attrs.get("url", "")
    inner = adf_to_text(node.get("content"))
    if kind == "listItem":
        return "- " + inner.strip() + "\n"
    if kind in ("paragraph", "heading", "codeBlock", "blockquote"):
        return inner.strip() + "\n\n"
    return inner


def summarize_issue(issue: dict[str, Any]) -> dict[str, Any]:
    f = issue.get("fields") or {}
    status = f.get("status") or {}
    category = (status.get("statusCategory") or {}).get("key", "")
    parent = f.get("parent") or {}
    return {
        "key": issue["key"],
        "summary": redact(f.get("summary") or ""),
        "type": (f.get("issuetype") or {}).get("name"),
        "status": status.get("name"),
        "stage": STAGE.get(category, category or "unknown"),
        "epic": (
            f"{parent['key']}: {(parent.get('fields') or {}).get('summary', '')}"
            if parent
            else None
        ),
        "labels": f.get("labels") or [],
        "fix_versions": [v.get("name") for v in f.get("fixVersions") or []],
        "resolved": (f.get("resolutiondate") or "")[:10] or None,
        "updated": (f.get("updated") or "")[:10] or None,
    }


class JiraClient:
    def __init__(self) -> None:
        base = (os.getenv("JIRA_BASE_URL") or "").strip().rstrip("/")
        email = (os.getenv("JIRA_EMAIL") or os.getenv("JIRA_API_EMAIL") or "").strip()
        token = (os.getenv("JIRA_API_TOKEN") or "").strip()
        if not (base and email and token):
            raise ValueError(
                "Jira isn't configured (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN), so I can't "
                "read tickets right now."
            )
        self.projects = allowed_projects()
        auth = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
        self._http = httpx.AsyncClient(
            base_url=base,
            headers={
                "Authorization": f"Basic {auth}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def __aenter__(self) -> "JiraClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._http.aclose()

    def _check(self, r: httpx.Response) -> Any:
        if r.status_code in (401, 403):
            raise ValueError(
                "Jira refused the request (check JIRA_EMAIL / JIRA_API_TOKEN and project access)."
            )
        if r.status_code == 404:
            raise ValueError("That Jira issue doesn't exist or the agent can't see it.")
        if r.status_code == 400:
            raise ValueError(f"Jira rejected the search: {r.text[:300]}")
        r.raise_for_status()
        return r.json()

    async def find(
        self, text: str | None = None, days: int | None = None, status: str = "any"
    ) -> dict[str, Any]:
        jql = build_jql(self.projects, text, days, status)
        data = self._check(
            await self._http.post(
                "/rest/api/3/search/jql",
                json={"jql": jql, "maxResults": MAX_ISSUES, "fields": _FIELDS},
            )
        )
        issues = [summarize_issue(i) for i in data.get("issues", [])]
        return {"issues": issues, "more": bool(data.get("nextPageToken"))}

    async def issue(self, key: str) -> dict[str, Any]:
        key = key.strip().upper()
        m = _KEY_RE.match(key)
        if not m or m["project"] not in self.projects:
            raise ValueError(f"{key!r} is not an issue in the allowed projects {self.projects}.")
        data = self._check(
            await self._http.get(
                f"/rest/api/3/issue/{key}", params={"fields": ",".join(_FIELDS + ["description"])}
            )
        )
        description = adf_to_text((data.get("fields") or {}).get("description")).strip()
        return {
            **summarize_issue(data),
            "description": clip(redact(description), DESCRIPTION_CHARS),
        }
