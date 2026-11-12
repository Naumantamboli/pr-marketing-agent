"""Catalog updater, step 1: turn merged PRs and docs changes into stored change events."""

import os
from datetime import timedelta
from typing import Any

from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger
from dotenv import load_dotenv

from utils import docs_site, events
from utils.github_client import GitHubClient
from utils.jira_client import JiraClient, allowed_projects

load_dotenv()
logger = setup_logger(__name__)


def _summary(event: dict[str, Any], key: str, notes: list[str]) -> dict[str, Any]:
    """What the workflow carries for an event; the evidence itself stays in storage."""
    return {
        "event_key": key,
        "event_id": event["event_id"],
        "kind": event["kind"],
        "title": event["title"],
        "url": event["url"],
        "occurred_at": event["occurred_at"],
        "notes": notes,
    }


async def _jira_issues(keys: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    if not keys:
        return [], []
    try:
        async with JiraClient() as jira:
            issues, skipped = await jira.issues(keys)
    except ValueError as err:  # Jira not configured: the PR still counts on its own
        return [], [f"Jira not read: {err}"]
    base = (os.getenv("JIRA_BASE_URL") or "").rstrip("/")
    return [{**i, "url": f"{base}/browse/{i['key']}"} for i in issues], skipped


@tool(name="fetch_pr_event_tool", start_to_close_timeout=timedelta(minutes=2))
async def fetch_pr_event_tool(pr_url: str) -> dict[str, Any]:
    """Fetch a merged PR and the Jira issues it mentions; store them as one event."""
    async with GitHubClient() as gh:
        pr = await gh.fetch_pr(pr_url)
    if not pr["merged"]:
        raise ValueError(f"{pr_url} is not merged.")
    keys = events.jira_keys(
        pr["title"], pr["head_branch"], pr["body"], projects=allowed_projects() or None
    )
    issues, notes = await _jira_issues(keys)
    doc = pr.get("feature_doc") or {}
    if doc.get("status") == "ok":
        notes.append(
            f"Feature doc linked but not read yet (Confluence client pending): {doc['url']}"
        )
    event = events.pr_event(pr, issues)
    return _summary(event, events.save_event(event), notes)


@tool(name="collect_docs_events_tool", start_to_close_timeout=timedelta(minutes=3))
async def collect_docs_events_tool(detected_on: str, shadow: bool = False) -> dict[str, Any]:
    """Diff the docs site against the last snapshot; store one event per changed page.

    The new snapshot is only staged; promote_docs_snapshot_tool makes it current once the
    run succeeds. With no previous snapshot this run just sets the baseline (the seed
    already describes the docs as they are).
    """
    pages = await docs_site.fetch_pages()
    previous = docs_site.load_snapshot(shadow)
    docs_site.stage_snapshot(pages, shadow)
    if not previous:
        logger.info("No docs snapshot yet; staged a baseline of %d pages.", len(pages))
        return {"events": [], "baseline": True, "pages": len(pages)}
    found = []
    for change in docs_site.diff_snapshots(previous, pages):
        event = events.docs_event(change, detected_on)
        found.append(_summary(event, events.save_event(event), []))
    logger.info("Docs: %d page(s) changed since the last snapshot.", len(found))
    return {"events": found, "baseline": False, "pages": len(pages)}


@tool(name="promote_docs_snapshot_tool")
async def promote_docs_snapshot_tool(shadow: bool = False) -> bool:
    return docs_site.promote_snapshot(shadow)
