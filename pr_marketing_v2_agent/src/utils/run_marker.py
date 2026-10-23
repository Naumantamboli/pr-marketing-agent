"""Choose which merged PRs a scheduled run processes, and where each repo's marker moves to.

A marker is {"merged_at": ISO timestamp, "numbers": [PR numbers merged at exactly that time
that were already handled]}. Keeping the numbers lets the next run re-read that timestamp
inclusively without processing the same PRs twice or skipping ties cut off by the limit.
"""

from datetime import datetime
from typing import Any


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def initial_marker(since: datetime) -> dict[str, Any]:
    return {"merged_at": since.isoformat(), "numbers": []}


def _already_handled(pr: dict[str, Any], marker: dict[str, Any]) -> bool:
    merged, mark = _iso(pr["merged_at"]), _iso(marker["merged_at"])
    return merged < mark or (merged == mark and pr["number"] in marker["numbers"])


def select_batch(
    found: dict[str, list[dict[str, Any]]],
    markers: dict[str, dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    """Walk PRs oldest first across repos; take up to `limit` eligible ones.

    Excluded PRs (fix/chore/...) don't count toward the limit but are still accounted for,
    so the marker moves past them. Everything after the cut-off is left for the next run.
    """
    pending = [
        {**pr, "repo": repo}
        for repo, prs in found.items()
        for pr in prs
        if not _already_handled(pr, markers[repo])
    ]
    pending.sort(key=lambda p: (_iso(p["merged_at"]), p["repo"], p["number"]))

    batch: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    accounted = 0
    for pr in pending:
        if pr["type_check"]["decision"] == "exclude":
            excluded.append(pr)
        elif len(batch) < limit:
            batch.append(pr)
        else:
            break
        accounted += 1

    new_markers = {repo: dict(marker) for repo, marker in markers.items()}
    for pr in pending[:accounted]:
        marker = new_markers[pr["repo"]]
        if _iso(pr["merged_at"]) > _iso(marker["merged_at"]):
            new_markers[pr["repo"]] = {"merged_at": pr["merged_at"], "numbers": [pr["number"]]}
        else:  # same timestamp: pending is sorted, so never earlier
            marker["numbers"] = [*marker["numbers"], pr["number"]]

    return {
        "prs": batch,
        "excluded": excluded,
        "truncated": accounted < len(pending),
        "markers": new_markers,
    }
