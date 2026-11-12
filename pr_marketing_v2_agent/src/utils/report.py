"""The run report: what a catalog update run looked at, changed, asked about and dropped."""

from typing import Any

_OP_LABEL = {
    "add_update": "Update",
    "edit_field": "Edit",
    "set_status": "Status",
    "upsert_component": "Component",
    "new_feature": "New feature",
}


def headline(p: dict[str, Any]) -> str:
    op = p["op"]
    target = op.get("name") or op.get("target")
    if op["op"] == "add_update":
        where = "component " if op.get("target_type") == "component" else ""
        return f"Update on {where}{op['target']} ({op.get('kind')})"
    if op["op"] == "edit_field":
        return f"Edit {op['target']}: {op['field']}"
    if op["op"] == "set_status":
        return f"Status of {op['target']} -> {op['status']}"
    if op["op"] == "upsert_component":
        return f"Component {target}"
    return f"{_OP_LABEL.get(op['op'], op['op'])}: {target}"


def review_item(p: dict[str, Any], max_chars: int = 600) -> dict[str, Any]:
    """The compact form a reviewer sees (kept small: the whole ask must fit in one signal)."""

    def cut(text: str) -> str:
        return text if len(text) <= max_chars else text[:max_chars] + " …"

    return {
        "id": p["id"],
        "tier": p["tier"],
        "headline": headline(p),
        "event_title": p["event_title"],
        "event_url": p["event_url"],
        "before": cut(p["preview"]["before"]),
        "after": cut(p["preview"]["after"]),
        "evidence": [
            {"quote": cut(c["quote"]), "source": c.get("source", c["evidence_id"])}
            for c in p["claims"][:3]
        ],
        "note": p.get("note", ""),
    }


def _lines(items: list[str], empty: str = "_None._") -> list[str]:
    return items or [empty]


def render(report: dict[str, Any]) -> str:
    r = report
    events = r.get("events", [])
    by_status: dict[str, int] = {}
    for e in events:
        by_status[e["status"]] = by_status.get(e["status"], 0) + 1
    counts = ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in sorted(by_status.items()))
    out = [
        f"# Catalog update — {r['started_on']} ({r['mode']} mode)",
        "",
        f"Run `{r['run_id']}`. Events: {len(events)} ({counts or 'none'}).",
    ]
    if r["mode"] == "shadow":
        out.append("Shadow mode: nothing below was written to the catalog.")
    if r.get("docs"):
        out.append(f"Docs: {r['docs']}")
    if r.get("excluded_prs"):
        out.append(f"PRs skipped by type (fix/chore/...): {r['excluded_prs']}.")

    out += ["", "## Applied", ""]
    out += _lines([f"- {a['headline']} — {a['after']}" for a in r.get("applied", [])])
    if r.get("not_applied"):
        out += ["", "## Not applied (catalog changed since proposed, or failed)", ""]
        out += [
            f"- {a['headline']}: {a['status']} — {a.get('error', '')}" for a in r["not_applied"]
        ]

    review = r.get("review") or {}
    out += ["", "## Review", ""]
    if review:
        out.append(
            f"Asked about {review.get('asked', 0)}: {review.get('approved', 0)} approved, "
            f"{review.get('rejected', 0)} rejected, {review.get('unanswered', 0)} unanswered, "
            f"{review.get('expired', 0)} expired. Pending after this run: "
            f"{r.get('pending_total', 0)}."
        )
    else:
        out.append("_Nothing needed review._")

    out += ["", "## Proposed this run", ""]
    proposed = r.get("proposals", [])
    for p in proposed:
        out.append(f"### [{p['tier']}] {headline(p)}")
        out.append(f"From [{p['event_title']}]({p['event_url']}) · {p.get('status', 'proposed')}")
        out.append("")
        if p["preview"]["before"] not in ("(none)", ""):
            out.append(f"- **Before:** {p['preview']['before']}")
        out.append(f"- **After:** {p['preview']['after']}")
        for c in p["claims"]:
            out.append(f'- Evidence ({c.get("source", c["evidence_id"])}): "{c["quote"]}"')
        if p.get("note"):
            out.append(f"- Note: {p['note']}")
        out.append("")
    if not proposed:
        out.append("_No changes proposed._")

    out += ["", "## Gaps (product changes without enough detail)", ""]
    out += _lines([f"- {g['event_id']}: {g['gap']}" for g in r.get("gaps", [])])

    out += ["", "## Overviews that may need a refresh", ""]
    out += _lines([f"- {s}" for s in r.get("refresh", [])])
    if r.get("overview_refresh"):
        out.append("- Product overview (features were added or changed status)")

    out += ["", "## Events", ""]
    for e in events:
        reason = f" — {e['reason']}" if e.get("reason") else ""
        out.append(f"- [{e['title']}]({e['url']}): {e['status'].replace('_', ' ')}{reason}")
    if not events:
        out.append("_No events._")

    out += ["", "## Dropped", ""]
    out += _lines(
        [
            f"- {d['event_id']} · {d['stage']}: {d['item']} — {d['reason']}"
            for d in r.get("dropped", [])
        ]
    )

    if r.get("failed"):
        out += ["", "## Failed (will be retried)", ""]
        out += [f"- {f.get('url') or f.get('event_key')}: {f['error']}" for f in r["failed"]]
    if r.get("given_up"):
        out += ["", "## Gave up after repeated failures", ""]
        out += [f"- {f.get('url') or f.get('event_key')}: {f['error']}" for f in r["given_up"]]
    return "\n".join(out) + "\n"
