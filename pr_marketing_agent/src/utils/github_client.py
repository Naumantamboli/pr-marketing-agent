"""Read-only GitHub REST client plus the diff sanitizer that guards what reaches the LLM."""

import os
import re
from datetime import datetime
from fnmatch import fnmatch
from typing import Any

import httpx
from common_lib.utils.logger import setup_logger

logger = setup_logger(__name__)

_PR_URL_RE = re.compile(r"https?://[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")
_CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE)

# Never sent to the LLM: noise, generated output, binaries, secrets-prone files.
SKIP_PATTERNS = [
    "*.lock",
    "*-lock.json",
    "*.lockb",
    "go.sum",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.snap",
    "*.svg",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.pdf",
    "*.zip",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.mp4",
    "dist/*",
    "build/*",
    "vendor/*",
    "node_modules/*",
    "*/dist/*",
    "*/build/*",
    "*/vendor/*",
    ".env*",
    "*/.env*",
    "*.pem",
    "*.key",
    "*.p12",
    "*secrets*",
]

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

PER_FILE_PATCH_CHARS = 4000


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    m = _PR_URL_RE.match(pr_url.strip())
    if not m:
        raise ValueError(f"Not a GitHub pull request URL: {pr_url!r}")
    return m["owner"], m["repo"], int(m["number"])


def redact(text: str) -> str:
    for rx in _SECRET_RES:
        text = rx.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text


def should_skip(path: str) -> bool:
    return any(fnmatch(path, p) or fnmatch(path.rsplit("/", 1)[-1], p) for p in SKIP_PATTERNS)


def sanitize_files(files: list[dict[str, Any]], budget: int) -> dict[str, Any]:
    """Keep the most informative patches within a char budget; list the rest by name only."""
    included, summarized, skipped = [], [], []
    used = 0
    # Smaller, non-test source changes first: they say the most per character.
    ordered = sorted(files, key=lambda f: ("test" in f["filename"].lower(), f.get("changes", 0)))
    for f in ordered:
        path = f["filename"]
        stats = {
            "path": path,
            "status": f.get("status"),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
        }
        if should_skip(path):
            skipped.append(stats)
            continue
        patch = f.get("patch")
        if not patch:
            summarized.append(stats)
            continue
        patch = redact(patch)
        if len(patch) > PER_FILE_PATCH_CHARS:
            patch = patch[:PER_FILE_PATCH_CHARS] + "\n… [patch truncated]"
        if used + len(patch) > budget:
            summarized.append(stats)
            continue
        used += len(patch)
        included.append({**stats, "patch": patch})
    return {"included": included, "summarized": summarized, "skipped": skipped, "chars": used}


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

    async def fetch_pr(self, pr_url: str, diff_budget: int) -> dict[str, Any]:
        owner, repo, number = parse_pr_url(pr_url)
        base = f"/repos/{owner}/{repo}"
        pr = await self._get(f"{base}/pulls/{number}")
        files = await self._paginate(f"{base}/pulls/{number}/files", limit=300)
        commits = await self._paginate(f"{base}/pulls/{number}/commits", limit=100)

        body = pr.get("body") or ""
        linked = []
        for issue_no in list(dict.fromkeys(_CLOSES_RE.findall(body)))[:5]:
            try:
                issue = await self._get(f"{base}/issues/{issue_no}")
                linked.append(
                    {
                        "number": int(issue_no),
                        "title": issue.get("title"),
                        "labels": [lb["name"] for lb in issue.get("labels", [])],
                    }
                )
            except (ValueError, httpx.HTTPError):
                logger.info("Linked issue #%s not readable; skipping.", issue_no)

        return {
            "url": pr["html_url"],
            "repo": f"{owner}/{repo}",
            "number": number,
            "title": pr["title"],
            "body": redact(body)[:8000],
            "author": (pr.get("user") or {}).get("login"),
            "labels": [lb["name"] for lb in pr.get("labels", [])],
            "draft": bool(pr.get("draft")),
            "merged": bool(pr.get("merged_at")),
            "merged_at": pr.get("merged_at"),
            "base_branch": pr["base"]["ref"],
            "files_changed": pr.get("changed_files", len(files)),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "commits": [redact(c["commit"]["message"].split("\n", 1)[0]) for c in commits],
            "linked_issues": linked,
            "diff": sanitize_files(files, diff_budget),
        }

    async def list_merged_prs(
        self, repo_full: str, since: datetime, limit: int
    ) -> list[dict[str, Any]]:
        """PRs merged into the default branch after `since`, oldest first."""
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
                    found.append({"url": pr["html_url"], "merged_at": merged_at})
            # Sorted by updated desc: once a page is entirely older than `since`, stop.
            if len(batch) < 100 or all(_iso(p["updated_at"]) <= since for p in batch):
                break
            page += 1
        found.sort(key=lambda p: p["merged_at"])
        return found[:limit]


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
