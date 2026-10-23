import os
from datetime import UTC, datetime, timedelta
from typing import Any

from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger
from dotenv import load_dotenv

from utils import storage
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
