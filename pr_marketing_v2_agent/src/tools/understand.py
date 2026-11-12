"""Catalog updater, steps 2-6: route, extract claims, verify them, compose operations, and
check those operations. Each tool loads the event by key; the LLM proposes, and code decides
what survives (quote matching, validation, status rules, tiers)."""

from datetime import timedelta
from typing import Any

from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger
from dotenv import load_dotenv

from utils import catalog, events, llm, policy, prompts, proposals, routing
from utils.quotes import quote_in
from utils.schemas import CheckResult, ComposeResult, ExtractResult, RouteResult, VerifyResult

load_dotenv()
logger = setup_logger(__name__)

_LLM = timedelta(minutes=4)


@tool(name="route_event_tool", start_to_close_timeout=_LLM)
async def route_event_tool(event_key: str) -> dict[str, Any]:
    """Which features and components the event touches, and whether it matters at all."""
    event = events.load_event(event_key)
    index, arch = catalog.load_index(), catalog.load_architecture()
    features = catalog.load_features(f["slug"] for f in index["features"])
    pages = routing.page_hits(event, features)
    text_hits = routing.feature_hits(event, index)
    component_hits = routing.component_hits(event, arch)

    hints = [*pages, *text_hits][:5] + component_hits[:3]
    result = await llm.structured(
        "route", prompts.SYSTEM, prompts.route(event, index, hints), RouteResult
    )
    feature_slugs = {f["slug"] for f in index["features"]}
    component_slugs = {c["slug"] for c in arch["components"]}
    # A changed page that a feature cites is a product change, whatever the model thinks.
    product_change = result.product_change or bool(pages)
    return {
        "event_id": event["event_id"],
        "product_change": product_change,
        "features": routing.merge(
            pages, result.features, text_hits, known=feature_slugs, limit=routing.MAX_FEATURES
        ),
        "components": routing.merge(
            result.components,
            component_hits,
            known=component_slugs,
            limit=routing.MAX_COMPONENTS,
        ),
        "new_feature": result.new_feature if product_change else None,
        "reason": result.reason,
    }


@tool(name="extract_claims_tool", start_to_close_timeout=_LLM)
async def extract_claims_tool(event_key: str, route: dict[str, Any]) -> dict[str, Any]:
    """Atomic claims, each with a verbatim quote; quotes that aren't in the evidence fail."""
    event = events.load_event(event_key)
    index, arch = catalog.load_index(), catalog.load_architecture()
    subjects = routing.subjects(route, index, arch)
    result = await llm.structured(
        "extract", prompts.SYSTEM, prompts.extract(event, subjects), ExtractResult
    )
    evidence = events.evidence_by_id(event)
    allowed = {s["about"] for s in subjects}
    kept, dropped = [], []
    for n, claim in enumerate(result.claims, start=1):
        c = {**claim.model_dump(), "id": f"c{n}"}
        source = evidence.get(c["evidence_id"])
        if not source:
            reason = f"cites unknown evidence {c['evidence_id']!r}"
        elif not quote_in(c["quote"], source["text"]):
            reason = "quote is not in the evidence"
        elif c["about"] not in allowed and not c["about"].startswith("new:"):
            reason = f"about unknown subject {c['about']!r}"
        else:
            kept.append({**c, "source": source["source"], "source_url": source.get("url")})
            continue
        dropped.append({"stage": "extract", "item": c["statement"], "reason": reason})
    return {"claims": kept, "dropped": dropped, "gaps": result.gaps}


@tool(name="verify_claims_tool", start_to_close_timeout=_LLM)
async def verify_claims_tool(event_key: str, claims: list[dict[str, Any]]) -> dict[str, Any]:
    """A second model, shown only each claim and its quote, decides whether it holds."""
    if not claims:
        return {"claims": [], "dropped": []}
    event = events.load_event(event_key)
    result = await llm.structured(
        "verify", prompts.SYSTEM, prompts.verify(event, claims), VerifyResult
    )
    verdicts = {v.claim_id: v for v in result.verdicts}
    kept, dropped = [], []
    for c in claims:
        v = verdicts.get(c["id"])
        if v and v.supported:
            kept.append(c)
        else:
            reason = v.reason if v else "no verdict returned"
            dropped.append({"stage": "verify", "item": c["statement"], "reason": reason})
    return {"claims": kept, "dropped": dropped}


def _targets(route: dict[str, Any], claims: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    features = list(route["features"])
    components = list(route["components"])
    for c in claims:
        about = c["about"]
        if about.startswith("component:"):
            components.append(about.split(":", 1)[1])
        elif not about.startswith("new:"):
            features.append(about)
    return list(dict.fromkeys(features)), list(dict.fromkeys(components))


@tool(name="compose_ops_tool", start_to_close_timeout=_LLM)
async def compose_ops_tool(
    event_key: str, route: dict[str, Any], claims: list[dict[str, Any]]
) -> dict[str, Any]:
    """Typed catalog operations built only from the verified claims."""
    event = events.load_event(event_key)
    feature_slugs, component_slugs = _targets(route, claims)
    features = catalog.load_features(feature_slugs)
    arch = catalog.load_architecture()
    components = [c for c in arch["components"] if c["slug"] in component_slugs]
    result = await llm.structured(
        "compose",
        prompts.SYSTEM,
        prompts.compose(event, claims, features, components),
        ComposeResult,
    )
    return {"ops": [op.model_dump() for op in result.ops]}


def _apply_check(op: dict[str, Any], check: Any) -> tuple[dict[str, Any] | None, str]:
    """Keep, narrow or drop an op according to the checker's verdict."""
    if check is None or check.verdict == "unsupported":
        return None, check.reason if check else "no check returned"
    if check.verdict == "supported" or op["op"] == "set_status":
        return op, ""
    revised = (check.revised_text or "").strip()
    field = {"add_update": "summary", "edit_field": "text", "upsert_component": "role"}.get(
        op["op"]
    )
    if field and revised:
        return {**op, field: revised}, f"narrowed by the checker: {check.reason}"
    if op["op"] == "new_feature":  # several texts; a reviewer sees the note (tier C)
        return op, f"checker says some text goes beyond the evidence: {check.reason}"
    return None, f"narrowed without a usable revision: {check.reason}"


@tool(name="check_ops_tool", start_to_close_timeout=_LLM)
async def check_ops_tool(
    event_key: str, ops: list[dict[str, Any]], claims: list[dict[str, Any]]
) -> dict[str, Any]:
    """Code checks, then a blind text-vs-claims check, then status rules and tiers.
    Returns proposals ready to queue."""
    event = events.load_event(event_key)
    index, arch = catalog.load_index(), catalog.load_architecture()
    feature_slugs = {f["slug"] for f in index["features"]}
    component_slugs = {c["slug"] for c in arch["components"]}
    by_id = {c["id"]: c for c in claims}

    candidates, dropped, notes = [], [], []
    for raw in ops:
        op = policy.normalize(raw)
        if op["op"] == "no_change":
            notes.append(op.get("reason") or "no change needed")
            continue
        problems = policy.validate(op, by_id, feature_slugs, component_slugs)
        if problems:
            dropped.append(
                {"stage": "check", "item": prompts.op_text(op), "reason": "; ".join(problems)}
            )
            continue
        current = catalog.load_feature(op["target"]) if op["target"] in feature_slugs else None
        op, why = policy.apply_status_rules(op, event, current)
        if op is None:
            dropped.append({"stage": "policy", "item": raw.get("op", ""), "reason": why})
            continue
        candidates.append(op)

    checks: dict[int, Any] = {}
    if candidates:
        result = await llm.structured(
            "check", prompts.SYSTEM, prompts.check(event, candidates, claims), CheckResult
        )
        checks = {c.index: c for c in result.checks}

    made, seen = [], set()
    for i, op in enumerate(candidates):
        kept, note = _apply_check(op, checks.get(i))
        if kept is None:
            dropped.append({"stage": "check", "item": prompts.op_text(op), "reason": note})
            continue
        problems = policy.validate(kept, by_id, feature_slugs, component_slugs)
        if problems:  # a narrowed text can still break a limit
            dropped.append(
                {"stage": "check", "item": prompts.op_text(kept), "reason": "; ".join(problems)}
            )
            continue
        feature = catalog.load_feature(kept["target"]) if kept["target"] in feature_slugs else None
        p = proposals.make_proposal(event, kept, by_id, policy.tier(kept), feature, arch, note)
        if p["id"] not in seen:
            seen.add(p["id"])
            made.append(p)
    return {"proposals": made, "dropped": dropped, "notes": notes}
