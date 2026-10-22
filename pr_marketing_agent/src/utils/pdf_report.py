"""Renders a run result as Markdown and as a PDF marketing report."""

import io
from html import escape
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

INK = colors.HexColor("#1B1F2A")
MUTED = colors.HexColor("#5B6170")
VERDICT_COLORS = {
    "post_worthy": colors.HexColor("#1F7A4D"),
    "release_notes_only": colors.HexColor("#2E5AAC"),
    "hold": colors.HexColor("#A86B00"),
    "internal": colors.HexColor("#6B7280"),
}
VERDICT_LABELS = {
    "post_worthy": "Post-worthy",
    "release_notes_only": "Release notes only",
    "hold": "Hold",
    "internal": "Internal (no content)",
}


def _title(result: dict[str, Any]) -> str:
    if result.get("mode") == "scheduled":
        p = result["period"]
        return f"Product update doc: {p['from'][:10]} to {p['to'][:10]}"
    return f"PR marketing report: {result['items'][0]['pr']['title']}"


# ── Markdown ────────────────────────────────────────────────────────────────


def _md_item(item: dict[str, Any]) -> list[str]:
    pr, v, s = item["pr"], item["verdict"], item.get("summary")
    out = [
        f"## {s['headline'] if s else pr['title']}",
        f"[{pr['repo']}#{pr['number']}]({pr['url']}) · **{VERDICT_LABELS[v['decision']]}** "
        f"· score {v['score']} · {v['category']}",
        "",
        "**Why this verdict:** " + "; ".join(v["reasons"]),
    ]
    if s:
        out += ["", s["plain_english"]]
        if s.get("who_benefits"):
            out += ["", f"**Who benefits:** {s['who_benefits']}"]
        out += ["", "**Verified facts**"] + [
            f"- {f['fact']} _(source: {f['source']})_" for f in s["verified_facts"]
        ]
        for label, key in (
            ("Assumptions (do not claim)", "assumptions"),
            ("Missing information", "missing_info"),
            ("Questions for the developer", "questions_for_dev"),
        ):
            if s.get(key):
                out += ["", f"**{label}**"] + [f"- {x}" for x in s[key]]
    for d in item.get("drafts", []):
        out += ["", f"### Draft: {d['format']} ({d['theme']}) · {d['status']}"]
        if d.get("flags"):
            out += [f"> Removed unsupported claims: {'; '.join(d['flags'])}"]
        if d.get("warnings"):
            out += [f"> {w}" for w in d["warnings"]]
        out += ["", d["content"]]
    return out


def render_markdown(result: dict[str, Any]) -> str:
    lines = [f"# {_title(result)}", ""]
    if result.get("mode") == "scheduled":
        t = result["totals"]
        lines += [
            f"Repos: {', '.join(result['repos'])}  ",
            f"Merged PRs: {t['merged']} · post-worthy {t['post_worthy']} · release-notes-only "
            f"{t['release_notes_only']} · hold {t['hold']} · internal {t['internal']}",
            "",
        ]
        if result.get("digest"):
            lines += ["## Weekly digest (draft)", "", result["digest"]["content"], ""]
        elif not result["items"]:
            lines += ["Nothing customer-facing shipped in this period.", ""]
    for item in result["items"]:
        lines += _md_item(item) + [""]
    if result.get("other_prs"):
        lines += ["## Also merged (no content)"] + [
            f"- [{p['title']}]({p['url']}): {VERDICT_LABELS[p['decision']]}, {p['reason']}"
            for p in result["other_prs"]
        ]
    lines += ["", "---", "_All drafts require human approval before publishing._"]
    return "\n".join(lines)


# ── PDF ─────────────────────────────────────────────────────────────────────


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "body",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13.5,
        textColor=INK,
        alignment=TA_LEFT,
    )
    return {
        "title": ParagraphStyle(
            "title", parent=body, fontName="Helvetica-Bold", fontSize=18, leading=22, spaceAfter=6
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=body,
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=17,
            spaceBefore=10,
            spaceAfter=4,
        ),
        "h3": ParagraphStyle(
            "h3",
            parent=body,
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=14,
            spaceBefore=8,
            spaceAfter=3,
        ),
        "body": body,
        "muted": ParagraphStyle("muted", parent=body, textColor=MUTED, fontSize=8.5, leading=12),
        "bullet": ParagraphStyle("bullet", parent=body, leftIndent=10, bulletIndent=0),
        "draft": ParagraphStyle(
            "draft",
            parent=body,
            fontName="Helvetica",
            backColor=colors.HexColor("#F5F6F8"),
            borderPadding=6,
            leftIndent=6,
            rightIndent=6,
            spaceBefore=4,
            spaceAfter=6,
        ),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


def _bullets(items: list[str], st: dict[str, ParagraphStyle]) -> list[Any]:
    return [Paragraph(escape(x), st["bullet"], bulletText="•") for x in items]


def _badge(decision: str, score: int, st: dict[str, ParagraphStyle]) -> Table:
    t = Table(
        [
            [
                Paragraph(
                    f"<b>{VERDICT_LABELS[decision]}</b> · score {score}",
                    ParagraphStyle("b", parent=st["body"], textColor=colors.white),
                )
            ]
        ]
    )
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), VERDICT_COLORS[decision]),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    t.hAlign = "LEFT"
    return t


def _pdf_item(item: dict[str, Any], st: dict[str, ParagraphStyle]) -> list[Any]:
    pr, v, s = item["pr"], item["verdict"], item.get("summary")
    flow: list[Any] = [
        _p(s["headline"] if s else pr["title"], st["h2"]),
        _p(f"{pr['repo']} #{pr['number']} · {pr['url']}", st["muted"]),
        Spacer(1, 3),
        _badge(v["decision"], v["score"], st),
        Spacer(1, 4),
        _p("Why: " + "; ".join(v["reasons"]), st["body"]),
    ]
    if s:
        flow += [Spacer(1, 4), _p(s["plain_english"], st["body"])]
        if s.get("who_benefits"):
            flow += [_p(f"Who benefits: {s['who_benefits']}", st["body"])]
        flow += [_p("Verified facts", st["h3"])]
        flow += _bullets([f"{f['fact']} (source: {f['source']})" for f in s["verified_facts"]], st)
        for label, key in (
            ("Assumptions (do not claim)", "assumptions"),
            ("Missing information", "missing_info"),
            ("Questions for the developer", "questions_for_dev"),
        ):
            if s.get(key):
                flow += [_p(label, st["h3"])] + _bullets(s[key], st)
    for d in item.get("drafts", []):
        block = [
            _p(f"Draft: {d['format'].replace('_', ' ')} ({d['theme']}) · needs approval", st["h3"])
        ]
        if d.get("flags"):
            block.append(_p("Removed unsupported claims: " + "; ".join(d["flags"]), st["muted"]))
        block += [_p(w, st["muted"]) for w in d.get("warnings", [])]
        block.append(_p(d["content"], st["draft"]))
        # Long drafts (blogs) must be allowed to split across pages.
        flow += [KeepTogether(block)] if len(d["content"]) < 1500 else block
    return flow


def _footer(canvas: Any, doc: Any) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 10 * mm, "Drafts require human approval before publishing.")
    canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


def build_pdf(result: dict[str, Any]) -> bytes:
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=_title(result),
    )
    flow: list[Any] = [_p(_title(result), st["title"])]
    if result.get("mode") == "scheduled":
        t = result["totals"]
        flow += [
            _p(f"Repos: {', '.join(result['repos'])}", st["muted"]),
            _p(
                f"Merged PRs: {t['merged']} · post-worthy {t['post_worthy']} · release-notes-only "
                f"{t['release_notes_only']} · hold {t['hold']} · internal {t['internal']}",
                st["body"],
            ),
        ]
        if result.get("digest"):
            flow += [
                _p("Weekly digest (draft)", st["h2"]),
                _p(result["digest"]["content"], st["draft"]),
            ]
        elif not result["items"]:
            flow += [
                Spacer(1, 8),
                _p("Nothing customer-facing shipped in this period.", st["body"]),
            ]
    if result.get("digest") and result["items"]:
        flow.append(PageBreak())
    for item in result["items"]:
        flow += [Spacer(1, 10)] + _pdf_item(item, st)
    if result.get("other_prs"):
        flow += [_p("Also merged (no content)", st["h2"])]
        flow += _bullets(
            [
                f"{p['title']} ({VERDICT_LABELS[p['decision']]}): {p['reason']}"
                for p in result["other_prs"]
            ],
            st,
        )
    doc.build(flow, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
