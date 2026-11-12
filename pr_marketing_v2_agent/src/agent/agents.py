"""ProductCatalogUpdater: keeps the product catalog in step with merged PRs and docs changes.

One scheduled run:
  collect   merged PRs since the marker (+ their Jira issues) and changed docs pages -> events
  per event route -> extract claims -> verify claims -> compose ops -> check ops -> proposals
  live mode queue proposals; apply tier A; advance markers; ask one review for tier B/C;
            apply what was approved
  report    markdown report (download link) + run audit in storage

Shadow mode runs everything except writing to the catalog, the review queue and the markers,
so its proposals can be judged before anything goes live.

Workflow code stays deterministic: no I/O, no clock, no env; every step is a tool.
"""

import asyncio
from datetime import timedelta
from typing import Any

from aetherion_sdk import Field, agent, agent_handle, humanInput, toolExecutor
from temporalio import workflow
from temporalio.common import RetryPolicy

NAME = "ProductCatalogUpdater"

_RETRY = RetryPolicy(maximum_attempts=3, non_retryable_error_types=["ValueError"])
_FAST = {"start_to_close_timeout": timedelta(minutes=2), "retry_policy": _RETRY}
_LLM = {
    "start_to_close_timeout": timedelta(minutes=4),
    "schedule_to_close_timeout": timedelta(minutes=15),
    "retry_policy": _RETRY,
}
_SLOW = {"start_to_close_timeout": timedelta(minutes=10), "retry_policy": _RETRY}

CHANNELS = ("slack", "teams", "sms", "whatsapp", "rcs")
PARALLEL_EVENTS = 4
MAX_ATTEMPTS = 3  # an event that fails this many runs in a row is reported and dropped
DEFAULTS = {
    "repos": [],
    "lookback_days": 7,
    "mode": "shadow",
    "include_docs": True,
    "reviewers": [],
    "review_hours": 24,
}


def _run_id() -> str:
    return workflow.info().workflow_id


def _today() -> str:
    return agent_handle.now().date().isoformat()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [v.strip() for v in value.replace("\n", ",").split(",") if v.strip()]
    return [str(v).strip() for v in value or [] if str(v).strip()]


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "on")
    return bool(value)


def settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Run options from the payload (run form or schedule), with safe defaults."""
    cfg = {**DEFAULTS, **{k: v for k, v in payload.items() if v not in (None, "")}}
    mode = str(cfg["mode"]).strip().lower()
    if mode not in ("shadow", "live"):
        raise ValueError(f"mode must be 'shadow' or 'live', not {cfg['mode']!r}.")
    return {
        "repos": _as_list(cfg["repos"]),
        "lookback_days": max(1, min(int(cfg["lookback_days"]), 90)),
        "live": mode == "live",
        "include_docs": _as_bool(cfg["include_docs"], True),
        "notify": notify_targets(cfg["reviewers"]),
        "review_hours": max(1, min(int(cfg["review_hours"]), 168)),
    }


def notify_targets(reviewers: Any) -> list[dict[str, str]]:
    """'slack:a@x.com, teams:b@x.com, c@x.com' -> notify entries (Slack when no channel)."""
    out = []
    for r in _as_list(reviewers):
        channel, sep, target = r.partition(":")
        if not sep or channel.lower() not in CHANNELS:
            channel, target = "slack", r
        out.append({"channel": channel.lower(), "target": target.strip()})
    return out


async def call(name: str, *args: Any, options: dict[str, Any] = _FAST) -> Any:
    return await toolExecutor.execute(name, *args, **options)


# --- per event -------------------------------------------------------------------------------


async def process_event(ev: dict[str, Any]) -> dict[str, Any]:
    """Route -> extract -> verify -> compose -> check for one event. Never raises: a failure
    comes back as status 'failed' so one bad event can't stop the run."""
    key = ev["event_key"]
    out: dict[str, Any] = {"event": ev, "proposals": [], "dropped": [], "gaps": [], "notes": []}
    try:
        route = await call("route_event_tool", key, options=_LLM)
        if not route["product_change"]:
            return {
                **out,
                "status": "skipped",
                "reason": route.get("reason") or "not a product change",
            }
        extracted = await call("extract_claims_tool", key, route, options=_LLM)
        out["gaps"], out["dropped"] = extracted["gaps"], list(extracted["dropped"])
        if not extracted["claims"]:
            return {**out, "status": "no_claims", "reason": "no quotable product facts"}
        verified = await call("verify_claims_tool", key, extracted["claims"], options=_LLM)
        out["dropped"] += verified["dropped"]
        if not verified["claims"]:
            return {**out, "status": "no_claims", "reason": "no claim survived verification"}
        composed = await call("compose_ops_tool", key, route, verified["claims"], options=_LLM)
        checked = await call(
            "check_ops_tool", key, composed["ops"], verified["claims"], options=_LLM
        )
        out["dropped"] += checked["dropped"]
        out["notes"] = checked["notes"]
        status = "proposed" if checked["proposals"] else "no_change"
        reason = "; ".join(checked["notes"]) if not checked["proposals"] else ""
        return {**out, "proposals": checked["proposals"], "status": status, "reason": reason}
    except Exception as exc:  # noqa: BLE001 - reported and retried next run
        workflow.logger.warning("Event %s failed: %s", ev.get("event_id"), exc)
        return {**out, "status": "failed", "reason": _error_text(exc)}


def _error_text(exc: BaseException) -> str:
    cause = exc
    while getattr(cause, "cause", None) is not None:
        cause = cause.cause
    return getattr(cause, "message", None) or str(cause) or type(cause).__name__


async def process_all(evs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for i in range(0, len(evs), PARALLEL_EVENTS):
        results += await asyncio.gather(*(process_event(e) for e in evs[i : i + PARALLEL_EVENTS]))
    return results


# --- review ----------------------------------------------------------------------------------


def review_form(items: list[dict[str, Any]]) -> tuple[str, list[Field]]:
    """One form for the whole batch: the details in the prompt, one approve/reject choice per
    item. Unanswered items stay pending for the next run."""
    lines = [
        f"The catalog updater proposes {len(items)} change(s) that need a person. Each is "
        "backed by the quoted evidence. Approve, reject, or leave blank to decide later.",
        "",
    ]
    fields = []
    for n, item in enumerate(items, start=1):
        lines += [
            f"**{n}. [{item['tier']}] {item['headline']}**",
            f"From: [{item['event_title']}]({item['event_url']})",
        ]
        if item["before"] not in ("(none)", ""):
            lines.append(f"Before: {item['before']}")
        lines.append(f"After: {item['after']}")
        lines += [f'> "{e["quote"]}" ({e["source"]})' for e in item["evidence"]]
        if item.get("note"):
            lines.append(f"Note: {item['note']}")
        lines.append("")
        fields.append(
            Field(
                name=f"p{n}",
                type="choice",
                friendly_name=f"{n}. {item['headline']}"[:80],
                options=(("Approve", "approve"), ("Reject", "reject")),
                required=False,
            )
        )
    fields.append(
        Field(
            name="notes",
            type="textarea",
            friendly_name="Why rejected (optional)",
            required=False,
        )
    )
    return "\n".join(lines), fields


async def ask_review(items: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    prompt, fields = review_form(items)
    response = await humanInput.request(
        prompt,
        fields=fields,
        metadata={"title": "Product catalog: changes to review"},
        notify=cfg["notify"] or None,
        timeout=timedelta(hours=cfg["review_hours"]),
        on_timeout="return_none",
    )
    if response is None:
        return {"decisions": {}, "notes": "", "reviewer": None}
    decisions = {item["id"]: response.value(f"p{n}") or "" for n, item in enumerate(items, 1)}
    return {
        "decisions": decisions,
        "notes": response.value("notes") or "",
        "reviewer": response.actor_id,
    }


# --- the run ---------------------------------------------------------------------------------


def _retry_entries(
    previous: list[dict[str, Any]], failed: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(entries to retry next run, entries given up on)."""
    attempts = {(e.get("url") or e.get("event_key")): e.get("attempts", 0) for e in previous}
    keep, give_up = [], []
    for f in failed:
        ident = f.get("url") or f.get("event_key")
        entry = {**f, "attempts": attempts.get(ident, 0) + 1}
        (give_up if entry["attempts"] >= MAX_ATTEMPTS else keep).append(entry)
    return keep, give_up


async def run_update(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = settings(payload)
    run_id, today = _run_id(), _today()
    live = cfg["live"]
    data: dict[str, Any] = {
        "run_id": run_id,
        "mode": "live" if live else "shadow",
        "started_on": today,
        "events": [],
        "proposals": [],
        "dropped": [],
        "gaps": [],
        "failed": [],
        "applied": [],
        "refresh": [],
    }

    # 1. Collect events.
    listed: dict[str, Any] = {"prs": [], "excluded": [], "markers": {}}
    if cfg["repos"]:
        listed = await call(
            "list_merged_prs_tool", cfg["repos"], cfg["lookback_days"], options=_SLOW
        )
        data["excluded_prs"] = len(listed["excluded"])
    retry = await call("load_retry_tool") if live else []

    pr_urls = list(
        dict.fromkeys(
            [p["url"] for p in listed["prs"]] + [r["url"] for r in retry if r.get("kind") == "pr"]
        )
    )
    events: list[dict[str, Any]] = [r["event"] for r in retry if r.get("kind") == "event"]
    failed: list[dict[str, Any]] = []
    for i in range(0, len(pr_urls), PARALLEL_EVENTS):
        batch = pr_urls[i : i + PARALLEL_EVENTS]
        found = await asyncio.gather(
            *(call("fetch_pr_event_tool", u) for u in batch), return_exceptions=True
        )
        for url, result in zip(batch, found, strict=True):
            if isinstance(result, BaseException):
                failed.append({"kind": "pr", "url": url, "error": _error_text(result)})
            else:
                events.append(result)

    docs_ok = False
    if cfg["include_docs"]:
        try:
            docs = await call("collect_docs_events_tool", today, not live, options=_SLOW)
            events += docs["events"]
            docs_ok = True
            data["docs"] = (
                f"baseline snapshot of {docs['pages']} pages taken"
                if docs["baseline"]
                else f"{len(docs['events'])} page(s) changed of {docs['pages']}"
            )
        except Exception as exc:  # noqa: BLE001 - PRs still get processed
            data["docs"] = f"not checked: {_error_text(exc)}"

    # 2. Understand each event.
    results = await process_all(events)
    proposals: list[dict[str, Any]] = []
    for r in results:
        ev = r["event"]
        data["events"].append({**ev, "status": r["status"], "reason": r.get("reason", "")})
        data["dropped"] += [{**d, "event_id": ev["event_id"]} for d in r["dropped"]]
        data["gaps"] += [{"event_id": ev["event_id"], "gap": g} for g in r["gaps"]]
        proposals += r["proposals"]
        if r["status"] == "failed":
            failed.append(
                {"kind": "event", "event": ev, "event_key": ev["event_key"], "error": r["reason"]}
            )
    data["proposals"] = proposals

    # 3. Queue, apply, review.
    queued = await call("queue_proposals_tool", run_id, proposals, live)
    outcomes: dict[str, str] = {}
    if live:
        if queued["auto"]:
            auto = await call("apply_proposals_tool", run_id, queued["auto"])
            _absorb(data, outcomes, auto)
        # Everything this run found is now queued or applied: move the markers on before
        # waiting on a person, so a slow review never holds back the next run.
        if listed["markers"]:
            await call("save_markers_tool", listed["markers"])
        if docs_ok:
            await call("promote_docs_snapshot_tool", False)
        keep, given_up = _retry_entries(retry, failed)
        await call("save_retry_tool", keep)
        data["given_up"] = given_up

        if queued["review"]:
            answer = await ask_review(queued["review"], cfg)
            asked = [i["id"] for i in queued["review"]]
            reviewed = await call(
                "record_review_tool",
                run_id,
                asked,
                answer["decisions"],
                answer["notes"],
                answer["reviewer"],
            )
            _absorb(data, outcomes, reviewed)
            for i in asked:
                if i not in outcomes:
                    rejected = answer["decisions"].get(i) == "reject"
                    outcomes[i] = "rejected" if rejected else "awaiting review"
            data["review"] = {
                "asked": len(asked),
                **{k: reviewed[k] for k in ("approved", "rejected", "unanswered", "expired")},
            }
        data["pending_total"] = await call("pending_count_tool")
    elif docs_ok:
        await call("promote_docs_snapshot_tool", True)

    default = "queued for review" if live else "not applied (shadow)"
    data["proposals"] = [{**p, "status": outcomes.get(p["id"], default)} for p in proposals]
    data["failed"] = failed
    data["refresh"] = list(dict.fromkeys(data["refresh"]))
    written = await call("write_report_tool", run_id, data)
    return result_shape(data, written["file_key"])


def _absorb(data: dict[str, Any], outcomes: dict[str, str], applied: dict[str, Any]) -> None:
    """Fold an apply result (from tier A or from review) into the report data."""
    for r in applied["results"]:
        outcomes[r["id"]] = r["status"]
        if r["status"] == "applied":
            data["applied"].append(r)
        else:
            data.setdefault("not_applied", []).append(r)
    data["refresh"] += applied["refresh"]
    data["overview_refresh"] = data.get("overview_refresh") or applied["overview_refresh"]


def result_shape(data: dict[str, Any], file_key: str) -> dict[str, Any]:
    events = data["events"]
    proposed = len(data["proposals"])
    applied = len(data["applied"])
    message = (
        f"{data['mode'].title()} run: {len(events)} event(s), {proposed} proposed change(s), "
        f"{applied} applied, {len(data['failed'])} failed."
    )
    if data.get("pending_total"):
        message += f" {data['pending_total']} waiting for review."
    return {
        "status": "success",
        "message": message,
        "results": [
            {
                "type": "s3_download_link",
                "title": "Catalog update report",
                "file_key": file_key,
                "label": "Download report",
                "extension": "md",
            }
        ],
    }


@agent(name=NAME, run_timeout=timedelta(days=8))
async def ProductCatalogUpdater(payload: dict[str, Any]) -> dict[str, Any]:
    """{"repos": "org/a, org/b", "lookback_days": 7, "mode": "shadow"|"live",
    "include_docs": true, "reviewers": "slack:owner@x.com", "review_hours": 24}"""
    return await run_update(payload or {})
