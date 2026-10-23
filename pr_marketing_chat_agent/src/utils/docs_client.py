"""Read-only GitHub client for the platform docs repo (lore): page index, page text, recent changes.

Only paths under DOCS_PATHS are visible to the agent, and only pages in the index can be read,
so the model can't use the token to fetch anything else from GitHub.
"""

import asyncio
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import yaml

from utils.text import clip, redact

PAGE_CHARS = 12000
_INDEX_CACHE: dict[str, list[dict[str, Any]]] = {}  # HEAD sha -> index; lives per worker
_DOC_EXT = (".md", ".mdx")
_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
# MDX module lines and JSX-only lines carry no prose.
_MDX_NOISE_RE = re.compile(
    r"^\s*(import|export)\s.*$|^\s*</?[A-Z][A-Za-z]*[^>]*>\s*$", re.MULTILINE
)


def _settings() -> tuple[str, str, list[str]]:
    repo = os.getenv("DOCS_REPO", "CalfusAgentFoundry/lore")
    branch = os.getenv("DOCS_BRANCH", "main")
    paths = [p.strip().strip("/") for p in os.getenv("DOCS_PATHS", "content/docs").split(",")]
    return repo, branch, [p for p in paths if p]


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[m.end() :]


def index_entry(path: str, text: str) -> dict[str, Any]:
    meta, body = split_front_matter(text)
    heading = _HEADING_RE.search(body)
    title = meta.get("title") or (heading.group(1).strip() if heading else path.rsplit("/", 1)[-1])
    return {"path": path, "title": str(title), "description": str(meta.get("description") or "")}


def readable(text: str) -> str:
    _, body = split_front_matter(text)
    body = _MDX_NOISE_RE.sub("", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def in_scope(path: str, roots: list[str]) -> bool:
    return path.endswith(_DOC_EXT) and any(path.startswith(r + "/") for r in roots)


class DocsClient:
    def __init__(self, token: str | None = None, api_url: str | None = None):
        token = token or os.getenv("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN is not set, so I can't read the docs repo.")
        self.repo, self.branch, self.roots = _settings()
        self._http = httpx.AsyncClient(
            base_url=(api_url or os.getenv("GITHUB_API_URL") or "https://api.github.com"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def __aenter__(self) -> "DocsClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._http.aclose()

    async def _get(self, url: str, raw: bool = False, **params: Any) -> Any:
        headers = {"Accept": "application/vnd.github.raw"} if raw else None
        r = await self._http.get(url, params=params or None, headers=headers)
        if r.status_code in (401, 403):
            raise ValueError(
                f"GitHub refused access to {self.repo} ({r.status_code}); check GITHUB_TOKEN."
            )
        if r.status_code == 404:
            raise ValueError(f"Not found in {self.repo}: {url}")
        r.raise_for_status()
        return r.text if raw else r.json()

    async def head_sha(self) -> str:
        return (await self._get(f"/repos/{self.repo}/commits/{self.branch}"))["sha"]

    async def _raw(self, path: str, ref: str) -> str:
        return await self._get(f"/repos/{self.repo}/contents/{path}", raw=True, ref=ref)

    async def index(self) -> tuple[str, list[dict[str, Any]]]:
        """(HEAD sha, [{path, title, description}]) for every doc page in scope."""
        sha = await self.head_sha()
        if sha in _INDEX_CACHE:
            return sha, _INDEX_CACHE[sha]
        tree = await self._get(f"/repos/{self.repo}/git/trees/{sha}", recursive=1)
        paths = sorted(
            e["path"]
            for e in tree["tree"]
            if e["type"] == "blob" and in_scope(e["path"], self.roots)
        )
        sem = asyncio.Semaphore(8)

        async def entry(path: str) -> dict[str, Any]:
            async with sem:
                return index_entry(path, await self._raw(path, sha))

        pages = list(await asyncio.gather(*(entry(p) for p in paths)))
        _INDEX_CACHE.clear()
        _INDEX_CACHE[sha] = pages
        return sha, pages

    async def read(self, path: str) -> dict[str, Any]:
        sha, pages = await self.index()
        page = next((p for p in pages if p["path"] == path.strip().lstrip("/")), None)
        if not page:
            raise ValueError(f"{path!r} is not a docs page; call list_docs for valid paths.")
        text = readable(await self._raw(page["path"], sha))
        return {**page, "content": clip(redact(text), PAGE_CHARS)}

    async def changes(self, days: int) -> list[dict[str, Any]]:
        """Commits touching the docs in the last `days` days, newest first, with pages changed."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        seen: dict[str, dict[str, Any]] = {}
        for root in self.roots:
            for c in await self._get(
                f"/repos/{self.repo}/commits", sha=self.branch, path=root, since=since, per_page=30
            ):
                seen.setdefault(c["sha"], c)
        commits = sorted(
            seen.values(), key=lambda c: c["commit"]["committer"]["date"], reverse=True
        )
        out = []
        for c in commits[:20]:
            detail = await self._get(f"/repos/{self.repo}/commits/{c['sha']}")
            out.append(
                {
                    "date": c["commit"]["committer"]["date"][:10],
                    "message": redact(c["commit"]["message"].split("\n", 1)[0]),
                    "pages": [
                        f["filename"]
                        for f in detail.get("files", [])
                        if in_scope(f["filename"], self.roots)
                    ],
                }
            )
        return out
