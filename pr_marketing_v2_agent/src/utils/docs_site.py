"""The public docs site as an event source: snapshot every page of llms-full.txt, and diff
against the previous snapshot to find pages that were added, changed or removed.

The docs describe what has shipped, so a docs change is the strongest evidence the catalog
gets. A snapshot is like a run marker: the new one is staged during a run and only becomes
current after the run succeeds (see promote_snapshot).
"""

import hashlib
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from utils import storage

_STATE = f"{storage.PREFIX}/state"


def snapshot_key(shadow: bool = False, pending: bool = False) -> str:
    """Shadow runs keep their own snapshot so they can be evaluated run after run without
    moving the live one."""
    name = "docs_snapshot" + (".shadow" if shadow else "") + (".pending" if pending else "")
    return f"{_STATE}/{name}.json"


_PAGE_HEAD_RE = re.compile(r"^\s*## (?P<title>.+?)\n+\*\*URL:\*\* (?P<url>\S+)\s*\n", re.DOTALL)
_DESCRIPTION_RE = re.compile(r"^\*\*Description:\*\*.*\n", re.MULTILINE)


def site_url() -> str:
    return os.getenv("DOCS_SITE_URL", "https://calfus.dev.aetherion.io/docs").rstrip("/")


def page_url(slug: str) -> str:
    return site_url() if slug == "index" else f"{site_url()}/{slug}"


def _slug(url: str) -> str:
    return urlsplit(url).path.strip("/").split("/")[-1] or "index"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def split_pages(full_text: str) -> dict[str, dict[str, Any]]:
    """llms-full.txt -> {slug: {slug, title, url, text, hash}}. Pages are separated by
    '---' lines and start with '## Title' then '**URL:** ...'."""
    pages: dict[str, dict[str, Any]] = {}
    for chunk in re.split(r"\n---\n", full_text):
        m = _PAGE_HEAD_RE.match(chunk)
        if not m:
            continue
        slug = _slug(m["url"])
        text = _DESCRIPTION_RE.sub("", chunk[m.end() :], count=1).strip()
        pages[slug] = {
            "slug": slug,
            "title": m["title"].strip(),
            "url": page_url(slug),
            "text": text,
            "hash": content_hash(text),
        }
    return pages


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def diff_page(old_text: str, new_text: str) -> tuple[list[str], list[str]]:
    """(added, removed) paragraphs, in page order. A reworded paragraph shows up in both."""
    old, new = paragraphs(old_text), paragraphs(new_text)
    old_set, new_set = set(old), set(new)
    return [p for p in new if p not in old_set], [p for p in old if p not in new_set]


def diff_snapshots(
    old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Pages added, changed or removed between two snapshots, with what changed."""
    changes = []
    for slug, page in new.items():
        before = old.get(slug)
        if before and before["hash"] == page["hash"]:
            continue
        added, removed = diff_page(before["text"] if before else "", page["text"])
        if not added and not removed:
            continue
        changes.append(
            {
                "slug": slug,
                "title": page["title"],
                "url": page["url"],
                "hash": page["hash"],
                "status": "changed" if before else "added",
                "added": added,
                "removed": removed,
            }
        )
    for slug, page in old.items():
        if slug not in new:
            changes.append(
                {
                    "slug": slug,
                    "title": page["title"],
                    "url": page["url"],
                    "hash": None,
                    "status": "removed",
                    "added": [],
                    "removed": paragraphs(page["text"]),
                }
            )
    return changes


async def fetch_pages() -> dict[str, dict[str, Any]]:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as http:
        r = await http.get(f"{site_url()}/llms-full.txt")
        r.raise_for_status()
    pages = split_pages(r.text)
    if not pages:
        raise ValueError(f"No pages found in {site_url()}/llms-full.txt; has its format changed?")
    return pages


def load_snapshot(shadow: bool = False) -> dict[str, dict[str, Any]] | None:
    return storage.get_json(snapshot_key(shadow))


def stage_snapshot(pages: dict[str, dict[str, Any]], shadow: bool = False) -> None:
    storage.put_json(snapshot_key(shadow, pending=True), pages)


def promote_snapshot(shadow: bool = False) -> bool:
    """Make the staged snapshot current; called once the run that read it has succeeded."""
    pending = storage.get_json(snapshot_key(shadow, pending=True))
    if not pending:
        return False
    storage.put_json(snapshot_key(shadow), pending)
    storage.put_json(snapshot_key(shadow, pending=True), {})
    return True
