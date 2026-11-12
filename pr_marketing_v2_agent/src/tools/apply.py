"""Catalog updater, steps 7-8: queue proposals, apply them (the single writer), record review
decisions, keep the retry list, and write the run report."""

from datetime import UTC, datetime
from typing import Any

from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger
from dotenv import load_dotenv

from utils import catalog, policy, proposals, report, storage

load_dotenv()
logger = setup_logger(__name__)

MAX_REVIEW_ITEMS = 15
RETRY_KEY = f"{storage.PREFIX}/state/catalog_retry.json"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _compact(p: dict[str, Any]) -> dict[str, Any]:
    out = {"id": p["id"], "status": p["status"], "headline": report.headline(p), "tier": p["tier"]}
    out["after"] = p["preview"]["after"]
    if p.get("error"):
        out["error"] = p["error"]
    return out


def _refresh(done: list[dict[str, Any]]) -> tuple[list[str], bool]:
    """Features whose overview may be out of date after these changes, and whether the
    product overview may be (features added or changed status)."""
    applied = [p for p in done if p["status"] == "applied"]
    slugs = {p["op"]["target"] for p in applied if p["op"].get("target_type") != "component"}
    stale = [f["slug"] for f in catalog.load_features(sorted(slugs)) if policy.needs_refresh(f)]
    product = any(p["op"]["op"] in ("new_feature", "set_status") for p in applied)
    return stale, product


def _apply(run_id: str, ids: list[str], decided_by: str | None) -> dict[str, Any]:
    taken = [{**p, "decided_by": decided_by} for p in proposals.take(ids)]
    done = proposals.apply(taken) if taken else []
    proposals.append_audit(run_id, "applied", done)
    refresh, product = _refresh(done)
    return {"results": [_compact(p) for p in done], "refresh": refresh, "overview_refresh": product}


@tool(name="queue_proposals_tool")
async def queue_proposals_tool(
    run_id: str, items: list[dict[str, Any]], live: bool
) -> dict[str, Any]:
    """Record every proposal in the run audit; in live mode also queue them. Returns the ids
    to apply automatically (tier A) and the oldest pending B/C items to ask a person about."""
    proposals.append_audit(run_id, "proposed", items)
    if not live:
        return {"auto": [], "review": [], "pending": 0}
    pending = proposals.queue(items)
    auto = [p["id"] for p in pending if p["tier"] == "A"]
    review = [report.review_item(p) for p in pending if p["tier"] != "A"][:MAX_REVIEW_ITEMS]
    return {"auto": auto, "review": review, "pending": len(pending) - len(auto)}


@tool(name="apply_proposals_tool")
async def apply_proposals_tool(run_id: str, ids: list[str]) -> dict[str, Any]:
    """Apply queued proposals without review (tier A)."""
    return _apply(run_id, ids, decided_by="auto")


@tool(name="record_review_tool")
async def record_review_tool(
    run_id: str,
    asked: list[str],
    decisions: dict[str, str],
    notes: str = "",
    reviewer: str | None = None,
) -> dict[str, Any]:
    """Apply approved proposals, close rejected ones, and age unanswered ones (expired after
    MAX_REVIEW_ROUNDS asks)."""
    approve = [i for i in asked if decisions.get(i) == "approve"]
    reject = [i for i in asked if decisions.get(i) == "reject"]
    unanswered = [i for i in asked if i not in approve and i not in reject]

    applied = _apply(run_id, approve, decided_by=reviewer or "reviewer")
    rejected = [
        {**p, "status": "rejected", "decided_by": reviewer, "reason": notes, "decided_at": _now()}
        for p in proposals.take(reject)
    ]
    proposals.append_audit(run_id, "rejected", rejected)

    pending, expired = [], []
    for p in proposals.load_pending():
        if p["id"] in unanswered:
            p = {**p, "review_rounds": p.get("review_rounds", 0) + 1}
            if p["review_rounds"] >= proposals.MAX_REVIEW_ROUNDS:
                expired.append({**p, "status": "expired", "decided_at": _now()})
                continue
        pending.append(p)
    proposals.save_pending(pending)
    proposals.append_audit(run_id, "expired", expired)
    return {
        **applied,
        "approved": len(approve),
        "rejected": len(rejected),
        "unanswered": len(unanswered) - len(expired),
        "expired": len(expired),
        "pending": len(pending),
    }


@tool(name="pending_count_tool")
async def pending_count_tool() -> int:
    return len(proposals.load_pending())


@tool(name="load_retry_tool")
async def load_retry_tool() -> list[dict[str, Any]]:
    return storage.get_json(RETRY_KEY) or []


@tool(name="save_retry_tool")
async def save_retry_tool(entries: list[dict[str, Any]]) -> None:
    storage.put_json(RETRY_KEY, entries)


@tool(name="write_report_tool")
async def write_report_tool(run_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Render the run report to markdown in storage; returns its key for a download link."""
    key = proposals.report_key(run_id)
    storage.put(key, report.render(data).encode(), "text/markdown")
    proposals.append_audit(run_id, "report", [{"key": key, "written_at": _now()}])
    return {"file_key": key}
