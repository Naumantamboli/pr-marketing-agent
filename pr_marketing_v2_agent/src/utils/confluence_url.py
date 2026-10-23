"""Find and validate the `Feature documentation:` Confluence link in a PR description.

Only the explicitly labeled link is used. Anything else (no label, several links, an
unapproved host, an unsupported URL shape) is flagged with a status instead of guessed.
"""

import os
import re
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlsplit

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# "Feature documentation: ...", "**Feature documentation:** ...", "- Feature documentation - ...",
# or a "## Feature documentation" heading with the link on the next line.
_LABEL_RE = re.compile(
    r"^[\s>#*_-]*feature\s+documentation[*_\s]*[:\-–—]?[*_\s]*(?P<rest>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\((?P<url>https?://[^)\s]+)\)")
_ANGLE_RE = re.compile(r"<(?P<url>https?://[^>\s]+)>")
_BARE_RE = re.compile(r"https?://[^\s<>()\[\]\"'`]+")

# Paths are matched after an optional context segment: "/wiki" on Cloud, e.g. "/confluence"
# on Server/DC.
_PREFIX = r"^(?:/[^/]+)?"
_PAGE_RE = re.compile(_PREFIX + r"/spaces/(?P<space>[^/]+)/pages/(?P<page_id>\d+)(?:/[^/]*)?/?$")
_VIEWPAGE_RE = re.compile(_PREFIX + r"/pages/viewpage\.action$")
_DISPLAY_RE = re.compile(_PREFIX + r"/display/(?P<space>[^/]+)/(?P<title>[^/]+)/?$")
_TINY_RE = re.compile(_PREFIX + r"/x/(?P<tiny>[A-Za-z0-9_-]+)/?$")


def _approved_hosts() -> set[str]:
    """CONFLUENCE_BASE_URL's host plus any in CONFLUENCE_ALLOWED_HOSTS (comma-separated)."""
    hosts = {
        h.strip().lower() for h in os.getenv("CONFLUENCE_ALLOWED_HOSTS", "").split(",") if h.strip()
    }
    base_host = urlsplit(os.getenv("CONFLUENCE_BASE_URL", "")).hostname
    if base_host:
        hosts.add(base_host.lower())
    return hosts


def _urls_in(text: str) -> list[str]:
    urls = [m["url"] for m in _MD_LINK_RE.finditer(text)]
    if not urls:
        urls = [m["url"] for m in _ANGLE_RE.finditer(text)]
    if not urls:
        # Sentence punctuation is not part of the link: "see https://.../Spec."
        urls = [m.group(0).rstrip(".,;:!?*_") for m in _BARE_RE.finditer(text)]
    return urls


def _labeled_urls(body: str) -> list[str]:
    """URLs given for the label: on its line, or on the next non-empty line."""
    body = _HTML_COMMENT_RE.sub("", body)
    lines = body.splitlines()
    found: list[str] = []
    for i, line in enumerate(lines):
        m = _LABEL_RE.match(line)
        if not m:
            continue
        urls = _urls_in(m["rest"])
        if not urls and not m["rest"].strip():
            following = next((ln for ln in lines[i + 1 :] if ln.strip()), "")
            urls = _urls_in(following)
        found += urls
    return list(dict.fromkeys(found))


def _result(status: str, reason: str, url: str | None = None, **ref: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "url": url,
        "page_id": ref.get("page_id"),
        "space_key": ref.get("space_key"),
        "page_title": ref.get("page_title"),
        "tiny_id": ref.get("tiny_id"),
    }


def extract_confluence_url(body: str) -> dict[str, Any]:
    """Validate the PR description's `Feature documentation:` link.

    `status` is "ok", "missing", "ambiguous", "host_not_approved" or "unsupported_format".
    On "ok", the page is identified by `page_id`, by `space_key` + `page_title`
    (Server/DC /display/ links), or by `tiny_id` (short /x/ links).
    """
    urls = _labeled_urls(body or "")
    if not urls:
        return _result("missing", "No URL after a 'Feature documentation:' label.")
    if len(urls) > 1:
        return _result("ambiguous", f"{len(urls)} different feature documentation URLs.")
    url = urls[0]

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in _approved_hosts():
        return _result(
            "host_not_approved", f"Host {host!r} is not an approved Confluence host.", url
        )
    if parts.scheme != "https" or parts.username or parts.port not in (None, 443):
        return _result(
            "unsupported_format", "Link must be plain https with no credentials or port.", url
        )

    path = parts.path
    if m := _PAGE_RE.match(path):
        return _result("ok", "Cloud page link.", url, page_id=m["page_id"], space_key=m["space"])
    if _VIEWPAGE_RE.match(path):
        page_id = (parse_qs(parts.query).get("pageId") or [""])[0]
        if page_id.isdigit():
            return _result("ok", "viewpage.action link.", url, page_id=page_id)
    if m := _DISPLAY_RE.match(path):
        return _result(
            "ok",
            "Space and title link.",
            url,
            space_key=m["space"],
            page_title=unquote_plus(m["title"]),
        )
    if m := _TINY_RE.match(path):
        return _result("ok", "Short link.", url, tiny_id=m["tiny"])
    return _result("unsupported_format", "Not a link to a single Confluence page.", url)
