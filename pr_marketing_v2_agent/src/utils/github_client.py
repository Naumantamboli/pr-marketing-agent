"""Read-only GitHub REST client for PR metadata (title, body, labels, branches, commits).

The marketing workflow never fetches changed files or diff patches.
"""

import os
import re
from datetime import datetime
from typing import Any

import httpx
from common_lib.utils.logger import setup_logger

from utils.confluence_url import extract_confluence_url

logger = setup_logger(__name__)

_PR_URL_RE = re.compile(r"https?://[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")

# Conventional-commit PR title, optionally after a ticket key: "feat(reports)!: ...",
# "[PROJ-123] perf: ...". With squash merges the title is what lands on the default branch.
_TITLE_TYPE_RE = re.compile(
    r"^\s*(?:\[?[A-Z][A-Z0-9_]+-\d+\]?[\s:-]*)?(?P<type>[a-z]+)(?:\([^)]*\))?(?P<bang>!)?:",
    re.IGNORECASE,
)
_BRANCH_TYPE_RE = re.compile(r"^(?P<type>[a-z]+)/", re.IGNORECASE)
_TYPE_ALIASES = {"feature": "feat", "performance": "perf", "bugfix": "fix", "hotfix": "fix"}

# Marketable change types; everything in _EXCLUDE_TYPES is dropped before any LLM call.
INCLUDE_TYPES = {
    t.strip().lower() for t in os.getenv("INCLUDE_TYPES", "feat,perf").split(",") if t.strip()
}
_EXCLUDE_TYPES = {
    "fix",
    "chore",
    "refactor",
    "test",
    "tests",
    "ci",
    "build",
    "docs",
    "style",
    "revert",
    "deps",
} - INCLUDE_TYPES
# A PR carrying this label is always included, whatever its type.
FORCE_LABEL = os.getenv("FORCE_INCLUDE_LABEL", "marketing").lower()

# PR bodies and commit messages can still paste credentials; scrub before they reach the LLM.
_SECRET_RES = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)((?:password|passwd|secret|api[_-]?key|token|client[_-]?secret)\s*[:=]\s*)"
        r"['\"]?[^'\"\s]{6,}"
    ),
]

PR_BODY_CHARS = 8000


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    m = _PR_URL_RE.match(pr_url.strip())
    if not m:
        raise ValueError(f"Not a GitHub pull request URL: {pr_url!r}")
    return m["owner"], m["repo"], int(m["number"])


def _change_type(regex: re.Pattern[str], text: str) -> str | None:
    m = regex.match(text or "")
    if not m:
        return None
    t = m["type"].lower()
    return _TYPE_ALIASES.get(t, t)


def check_pr_type(title: str, branch: str, labels: list[str], body: str = "") -> dict[str, Any]:
    """Cheap pre-filter: include / exclude by PR title, then branch prefix.

    "unknown" means neither says; the Jira issue type and then LLM triage decide.
    """
    if FORCE_LABEL in {lb.lower() for lb in labels}:
        return {"decision": "include", "type": None, "source": "label"}
    title_match = _TITLE_TYPE_RE.match(title or "")
    # Breaking changes are always announced, even when typed as fix/refactor.
    if (title_match and title_match["bang"]) or "BREAKING CHANGE" in (body or ""):
        t = _change_type(_TITLE_TYPE_RE, title)
        return {"decision": "include", "type": t, "source": "breaking"}
    for source, t in (
        ("title", _change_type(_TITLE_TYPE_RE, title)),
        ("branch", _change_type(_BRANCH_TYPE_RE, branch)),
    ):
        if t in INCLUDE_TYPES:
            return {"decision": "include", "type": t, "source": source}
        if t in _EXCLUDE_TYPES:
            return {"decision": "exclude", "type": t, "source": source}
    return {"decision": "unknown", "type": None, "source": None}


def redact(text: str) -> str:
    for rx in _SECRET_RES:
        text = rx.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text


class GitHubClient:
    def __init__(self, token: str | None = None, api_url: str | None = None):
        token = token or os.getenv("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN is not set.")
        self._http = httpx.AsyncClient(
            base_url=(api_url or os.getenv("GITHUB_API_URL") or "https://api.github.com"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        r = await self._http.get(path, params=params or None)
        if r.status_code == 404:
            raise ValueError(f"GitHub returned 404 for {path} (missing, or token lacks access).")
        r.raise_for_status()
        return r.json()

    async def _paginate(self, path: str, limit: int, **params: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while len(items) < limit:
            batch = await self._get(path, per_page=100, page=page, **params)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items[:limit]

    async def fetch_pr(self, pr_url: str) -> dict[str, Any]:
        """PR metadata and commit messages; no changed-files or patch requests."""
        owner, repo, number = parse_pr_url(pr_url)
        base = f"/repos/{owner}/{repo}"
        pr = await self._get(f"{base}/pulls/{number}")
        commits = await self._paginate(f"{base}/pulls/{number}/commits", limit=100)

        body = pr.get("body") or ""
        labels = [lb["name"] for lb in pr.get("labels", [])]
        return {
            "url": pr["html_url"],
            "repo": f"{owner}/{repo}",
            "number": number,
            "title": pr["title"],
            "body": redact(body)[:PR_BODY_CHARS],
            "author": (pr.get("user") or {}).get("login"),
            "labels": labels,
            "draft": bool(pr.get("draft")),
            "merged": bool(pr.get("merged_at")),
            "merged_at": pr.get("merged_at"),
            "base_branch": pr["base"]["ref"],
            # Source branch; usually carries the Jira key (feat/PROJ-123-...).
            "head_branch": pr["head"]["ref"],
            "type_check": check_pr_type(pr["title"], pr["head"]["ref"], labels, body),
            # Size counters come with the PR object itself; no extra request.
            "files_changed": pr.get("changed_files", 0),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "commits": [redact(c["commit"]["message"].split("\n", 1)[0]) for c in commits],
            # Read from the full body, before truncation; status != "ok" flags the PR.
            "feature_doc": extract_confluence_url(body),
        }

    async def list_merged_prs(
        self, repo_full: str, since: datetime, limit: int
    ) -> list[dict[str, Any]]:
        """PRs merged into the default branch after `since`, oldest first.

        Each carries `type_check`, so fix/chore PRs can be dropped before `fetch_pr`.
        """
        meta = await self._get(f"/repos/{repo_full}")
        default_branch = meta["default_branch"]
        found: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = await self._get(
                f"/repos/{repo_full}/pulls",
                state="closed",
                base=default_branch,
                sort="updated",
                direction="desc",
                per_page=100,
                page=page,
            )
            for pr in batch:
                merged_at = pr.get("merged_at")
                if merged_at and _iso(merged_at) > since:
                    labels = [lb["name"] for lb in pr.get("labels", [])]
                    found.append(
                        {
                            "url": pr["html_url"],
                            "merged_at": merged_at,
                            "title": pr["title"],
                            "head_branch": pr["head"]["ref"],
                            "labels": labels,
                            "type_check": check_pr_type(
                                pr["title"], pr["head"]["ref"], labels, pr.get("body") or ""
                            ),
                        }
                    )
            # Sorted by updated desc: once a page is entirely older than `since`, stop.
            if len(batch) < 100 or all(_iso(p["updated_at"]) <= since for p in batch):
                break
            page += 1
        found.sort(key=lambda p: p["merged_at"])
        return found[:limit]


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
