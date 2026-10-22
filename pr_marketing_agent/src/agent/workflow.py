import asyncio
from datetime import timedelta
from typing import Any

from aetherion_sdk import agent, toolExecutor
from common_lib.utils.logger import setup_logger
from temporalio.common import RetryPolicy

logger = setup_logger(__name__)

# Bad input (bad URL, unknown format, 404) won't fix itself on retry.
_RETRY = RetryPolicy(maximum_attempts=3, non_retryable_error_types=["ValueError"])
_FAST = {"start_to_close_timeout": timedelta(minutes=3), "retry_policy": _RETRY}
_LLM = {"start_to_close_timeout": timedelta(minutes=6), "retry_policy": _RETRY}
_LONG = {"start_to_close_timeout": timedelta(minutes=12), "retry_policy": _RETRY}
_CONCURRENCY = 5
_WORTHY = ("post_worthy", "release_notes_only")


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = value.replace("\n", ",").split(",")
    return [v.strip() for v in value if v and v.strip()]


def _as_bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "1", "yes")


def _pr_meta(pr: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "url",
        "repo",
        "number",
        "title",
        "author",
        "merged",
        "merged_at",
        "draft",
        "files_changed",
        "labels",
    )
    return {k: pr[k] for k in keys}


async def _bounded(coros: list[Any]) -> list[Any]:
    out: list[Any] = []
    for i in range(0, len(coros), _CONCURRENCY):
        out += await asyncio.gather(*coros[i : i + _CONCURRENCY])
    return out


async def _draft(fmt: str, opts: dict[str, Any], sources: list[dict], period: str | None) -> dict:
    timeouts = _LONG if fmt == "blog_draft" else _LLM
    draft = await toolExecutor.execute(
        "generate_content_tool",
        fmt,
        opts["theme"],
        sources,
        opts["extra_context"],
        period,
        **timeouts,
    )
    return await toolExecutor.execute("verify_draft_tool", draft, sources, **_LLM)


async def _screen(url: str, opts: dict[str, Any], use_recommended: bool) -> tuple[dict, dict]:
    pr = await toolExecutor.execute("fetch_pr_tool", url, **_FAST)
    verdict = await toolExecutor.execute(
        "triage_tool", pr, opts["formats"], opts["force"], use_recommended, **_LLM
    )
    return pr, verdict


async def _build_item(pr: dict, verdict: dict, opts: dict[str, Any]) -> dict[str, Any]:
    analysis = await toolExecutor.execute("analyze_tool", pr, opts["extra_context"], **_LLM)
    sources = [{"pr": _pr_meta(pr), "analysis": analysis, "category": verdict["category"]}]
    drafts = await asyncio.gather(
        *(_draft(f, opts, sources, None) for f in verdict["formats_to_generate"])
    )
    return {"pr": _pr_meta(pr), "verdict": verdict, "summary": analysis, "drafts": list(drafts)}


async def _on_demand(opts: dict[str, Any]) -> dict[str, Any]:
    if not opts["pr_url"]:
        raise ValueError("pr_url is required for on-demand runs.")
    pr, verdict = await _screen(opts["pr_url"], opts, use_recommended=True)
    item = await _build_item(pr, verdict, opts)
    result: dict[str, Any] = {"mode": "on_demand", "items": [item]}
    result["report"] = await toolExecutor.execute("render_report_tool", result, **_FAST)

    d = verdict["decision"]
    if item["drafts"]:
        message = (
            f"{d.replace('_', ' ').title()} (score {verdict['score']}): "
            f"{len(item['drafts'])} draft(s) ready for review."
        )
    else:
        message = f"{d.replace('_', ' ').title()} (score {verdict['score']}): no content generated."
    return {
        **result,
        "status": "awaiting_human_approval" if item["drafts"] else "no_content",
        "message": message,
    }


async def _scheduled(opts: dict[str, Any]) -> dict[str, Any]:
    if not opts["repos"]:
        raise ValueError("repos is required for scheduled runs (owner/name, one per line).")
    listing = await toolExecutor.execute(
        "list_merged_prs_tool", opts["repos"], opts["lookback_days"], **_FAST
    )
    screened = await _bounded(
        [_screen(p["url"], opts, use_recommended=False) for p in listing["prs"]]
    )
    keep = [(pr, v) for pr, v in screened if v["decision"] in _WORTHY or opts["force"]]
    others = [
        {
            "title": pr["title"],
            "url": pr["url"],
            "decision": v["decision"],
            "reason": v["reasons"][0],
        }
        for pr, v in screened
        if not (v["decision"] in _WORTHY or opts["force"])
    ]
    items = await _bounded([_build_item(pr, v, opts) for pr, v in keep])
    # Highlights first in the doc.
    items.sort(key=lambda i: (i["verdict"]["decision"] != "post_worthy", -i["verdict"]["score"]))

    period = listing["period"]
    digest = None
    if items:
        sources = [
            {"pr": i["pr"], "analysis": i["summary"], "category": i["verdict"]["category"]}
            for i in items
        ]
        digest = await _draft(
            "weekly_digest", opts, sources, f"{period['from'][:10]} to {period['to'][:10]}"
        )

    totals = {"merged": len(screened), **{d: 0 for d in (*_WORTHY, "hold", "internal")}}
    for _, v in screened:
        totals[v["decision"]] += 1
    result: dict[str, Any] = {
        "mode": "scheduled",
        "period": period,
        "repos": opts["repos"],
        "totals": totals,
        "truncated": listing["truncated"],
        "digest": digest,
        "items": items,
        "other_prs": others,
    }
    result["report"] = await toolExecutor.execute("render_report_tool", result, **_FAST)
    # Only after everything succeeded, so a failed run re-covers the same window.
    await toolExecutor.execute("save_cursors_tool", listing["cursors"], **_FAST)

    t = result["totals"]
    message = (
        f"{t['merged']} merged PR(s): {t['post_worthy']} post-worthy, "
        f"{t['release_notes_only']} release-notes-only, {t['hold']} on hold, "
        f"{t['internal']} internal."
        + (" More PRs are waiting; the next run continues." if listing["truncated"] else "")
    )
    return {
        **result,
        "status": "awaiting_human_approval" if items else "nothing_to_announce",
        "message": message,
    }


@agent(name="PrMarketingAgent")
async def pr_marketing_agent(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn GitHub PRs into a post-worthiness verdict, a plain-English summary and
    marketing drafts for human approval.

    on_demand: {"pr_url": ...}. scheduled (cron): {"mode": "scheduled", "repos": [...]}.
    Optional: formats, theme, force_generate, extra_context, lookback_days.
    """
    mode = (payload.get("mode") or ("scheduled" if payload.get("repos") else "on_demand")).strip()
    opts = {
        "pr_url": (payload.get("pr_url") or "").strip(),
        "repos": _as_list(payload.get("repos")),
        "formats": _as_list(payload.get("formats")),
        "theme": (payload.get("theme") or "").strip() or None,
        "force": _as_bool(payload.get("force_generate")),
        "extra_context": (payload.get("extra_context") or "").strip() or None,
        "lookback_days": int(payload.get("lookback_days") or 7),
    }
    logger.info("PrMarketingAgent run: mode=%s", mode)
    if mode == "scheduled":
        return await _scheduled(opts)
    if mode == "on_demand":
        return await _on_demand(opts)
    raise ValueError(f"Unknown mode {mode!r}; use on_demand or scheduled.")
