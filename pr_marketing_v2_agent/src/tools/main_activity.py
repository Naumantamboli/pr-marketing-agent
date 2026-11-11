import os
from datetime import UTC, datetime, timedelta
from typing import Any

from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger
from dotenv import load_dotenv

from utils import catalog, memsy_store, overview, storage
from utils.github_client import GitHubClient
from utils.run_marker import initial_marker, select_batch

load_dotenv()
logger = setup_logger(__name__)

# Eligible PRs (not excluded by type) processed per scheduled run; the rest wait for the next.
MAX_PRS_PER_RUN = int(os.getenv("MAX_PRS_PER_RUN", "30"))
# Safety cap on PRs listed per repo per run; the oldest are kept, so none are skipped.
MAX_CANDIDATES_PER_REPO = int(os.getenv("MAX_CANDIDATES_PER_REPO", "500"))


@tool(name="list_merged_prs_tool")
async def list_merged_prs_tool(repos: list[str], lookback_days: int) -> dict[str, Any]:
    """PRs merged since each repo's run marker (or lookback_days on the first run).

    Returns the batch to process, the excluded PRs, and the markers to save once the
    run succeeds.
    """
    now = datetime.now(UTC)
    found: dict[str, list[dict[str, Any]]] = {}
    markers: dict[str, dict[str, Any]] = {}
    since_by_repo: dict[str, datetime] = {}
    async with GitHubClient() as gh:
        for repo in repos:
            # First run: anchor at the lookback and save it, so later runs never re-derive
            # the window from the clock and leave a gap.
            markers[repo] = storage.read_marker(repo) or initial_marker(
                now - timedelta(days=lookback_days)
            )
            since = datetime.fromisoformat(markers[repo]["merged_at"].replace("Z", "+00:00"))
            since_by_repo[repo] = since
            found[repo] = await gh.list_merged_prs(repo, since, MAX_CANDIDATES_PER_REPO)
            logger.info("%s: %d PRs merged since %s", repo, len(found[repo]), since.isoformat())

    result = select_batch(found, markers, MAX_PRS_PER_RUN)
    logger.info(
        "Selected %d PR(s), excluded %d, truncated=%s",
        len(result["prs"]),
        len(result["excluded"]),
        result["truncated"],
    )
    earliest = min(since_by_repo.values(), default=now)
    return {**result, "period": {"from": earliest.isoformat(), "to": now.isoformat()}}


@tool(name="save_markers_tool")
async def save_markers_tool(markers: dict[str, dict[str, Any]]) -> None:
    """Advance run markers; called only after a run succeeds, so a failed run re-covers
    the same PRs."""
    for repo, marker in markers.items():
        storage.write_marker(repo, {**marker, "saved_at": datetime.now(UTC).isoformat()})


# ── Product knowledge (read side): overview + catalog in storage, features in Memsy ──


@tool(name="get_overview_tool")
async def get_overview_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """The published Product Overview, or {"available": False} before the first one."""
    found = overview.get_published()
    return {"available": True, "overview": found} if found else {"available": False}


@tool(name="list_features_tool")
async def list_features_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Every published feature: slug, name, aliases, one-line summary."""
    return {"features": catalog.listing(catalog.load())}


@tool(name="find_feature_tool")
async def find_feature_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Features named in the text (by slug, name or alias), best match first."""
    return {"matches": catalog.find(catalog.load(), str(payload.get("text") or ""))}


@tool(name="search_feature_knowledge_tool")
async def search_feature_knowledge_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Current facts for a question: within one feature when `feature` is given, otherwise
    across all features. Only facts the catalog lists as current are returned."""
    query = str(payload.get("query") or "").strip()
    if not query:
        raise ValueError("search_feature_knowledge needs a query.")
    limit = max(1, min(int(payload.get("limit") or 8), 20))
    slug = payload.get("feature")
    current = catalog.load()
    async with memsy_store.client() as memsy:
        if slug:
            results = await memsy_store.search_feature(memsy, current, slug, query, limit)
        else:
            results = await memsy_store.search_all(memsy, current, query, limit)
    return {"feature": slug, "results": results}
