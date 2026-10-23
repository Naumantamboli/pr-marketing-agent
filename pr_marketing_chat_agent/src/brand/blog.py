"""Blog draft (simple Markdown) -> branded .docx (editable) and .pdf (for review/sharing)."""

import io
import re
from html import escape
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Mm, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer

ASSETS = Path(__file__).parent / "assets"
MINT = "3ECFA0"
INK = "080808"
MUTED = "4C4C4C"
FOOTER = "Calfus Inc. · www.calfus.com"

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")


def parse(markdown: str) -> tuple[str, list[tuple[str, str]]]:
    """Title + blocks of (kind, text); kind is h2, h3, bullet or p."""
    title, blocks, para = "", [], []

    def flush() -> None:
        if para:
            blocks.append(("p", " ".join(para)))
            para.clear()

    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("# ") and not title:
            flush()
            title = line[2:].strip()
        elif line.startswith("### "):
            flush()
            blocks.append(("h3", line[4:].strip()))
        elif line.startswith("## ") or line.startswith("# "):
            flush()
            blocks.append(("h2", line.lstrip("#").strip()))
        elif re.match(r"^([-*•]|\d+[.)])\s+", line):
            flush()
            blocks.append(("bullet", re.sub(r"^([-*•]|\d+[.)])\s+", "", line)))
        else:
            para.append(line)
    flush()
    return title or "Blog draft", blocks


def _runs(text: str) -> list[tuple[str, bool, bool]]:
    """Split **bold** / *italic* spans into (text, bold, italic) runs."""
    out: list[tuple[str, bool, bool]] = []
    for i, part in enumerate(_BOLD_RE.split(text)):
        bold = i % 2 == 1
        for j, sub in enumerate(_ITALIC_RE.split(part)):
            if sub:
                out.append((sub, bold, j % 2 == 1))
    return out


# ── DOCX ────────────────────────────────────────────────────────────────────


def _style_run(
    run: Any, size: float, color: str, bold: bool = False, font: str = "DM Sans"
) -> None:
    run.font.name = font
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)


def build_docx(markdown: str) -> bytes:
    title, blocks = parse(markdown)
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.left_margin = sec.right_margin = Mm(22)
    sec.header.paragraphs[0].add_run().add_picture(str(ASSETS / "logo_black.png"), height=Mm(9))
    foot = sec.footer.paragraphs[0]
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _style_run(foot.add_run(FOOTER), 8, MUTED)

    normal = doc.styles["Normal"]
    normal.font.name = "DM Sans"
    normal.font.size = Pt(11)

    _style_run(doc.add_paragraph().add_run(title), 26, INK, bold=True, font="Stack Sans Notch")
    rule = doc.add_paragraph()
    _style_run(rule.add_run("━━━━━━"), 12, MINT)
    for kind, text in blocks:
        if kind in ("h2", "h3"):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(14 if kind == "h2" else 10)
            _style_run(
                p.add_run(text), 16 if kind == "h2" else 13, MINT if kind == "h2" else INK, True
            )
            continue
        p = doc.add_paragraph(style="List Bullet" if kind == "bullet" else None)
        p.paragraph_format.space_after = Pt(6)
        for chunk, bold, italic in _runs(text):
            run = p.add_run(chunk)
            _style_run(run, 11, INK, bold)
            run.font.italic = italic
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# ── PDF ─────────────────────────────────────────────────────────────────────

_FONTS = {
    "DMSans": "DMSans-Regular.ttf",
    "DMSans-Bold": "DMSans-Bold.ttf",
    "DMSans-SemiBold": "DMSans-SemiBold.ttf",
    "StackSansNotch-Bold": "StackSansNotch-Bold.ttf",
}


def _register_fonts() -> None:
    for name, file in _FONTS.items():
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(ASSETS / "fonts" / file)))
    pdfmetrics.registerFontFamily("DMSans", normal="DMSans", bold="DMSans-Bold")


def _inline(text: str) -> str:
    html = ""
    for chunk, bold, italic in _runs(text):
        piece = escape(chunk)
        # DM Sans ships no italic here; keep emphasis as semibold weight.
        if bold or italic:
            piece = f"<b>{piece}</b>"
        html += piece
    return html


def _footer(canvas: Any, doc: Any) -> None:
    canvas.saveState()
    canvas.setFont("DMSans", 8)
    canvas.setFillColor(colors.HexColor(f"#{MUTED}"))
    canvas.drawString(22 * mm, 12 * mm, FOOTER)
    canvas.drawRightString(A4[0] - 22 * mm, 12 * mm, f"Draft · page {doc.page}")
    canvas.restoreState()


def build_pdf(markdown: str) -> bytes:
    _register_fonts()
    title, blocks = parse(markdown)
    ink = colors.HexColor(f"#{INK}")
    body = ParagraphStyle("body", fontName="DMSans", fontSize=10.5, leading=15.5, textColor=ink)
    st = {
        "title": ParagraphStyle(
            "title", parent=body, fontName="StackSansNotch-Bold", fontSize=26, leading=31
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=body,
            fontName="DMSans-Bold",
            fontSize=15,
            leading=19,
            spaceBefore=12,
            spaceAfter=4,
            textColor=colors.HexColor(f"#{MINT}"),
        ),
        "h3": ParagraphStyle(
            "h3", parent=body, fontName="DMSans-SemiBold", fontSize=12, spaceBefore=8, spaceAfter=3
        ),
        "p": ParagraphStyle("p", parent=body, spaceAfter=7),
        "bullet": ParagraphStyle("bullet", parent=body, leftIndent=12, spaceAfter=4),
    }
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=22 * mm,
        rightMargin=22 * mm,
        topMargin=18 * mm,
        bottomMargin=22 * mm,
        title=title,
        author="Calfus Inc.",
    )
    logo = Image(str(ASSETS / "logo_black.png"), width=30 * mm, height=10 * mm)
    logo.hAlign = "LEFT"
    flow: list[Any] = [
        logo,
        Spacer(1, 14),
        Paragraph(escape(title), st["title"]),
        Spacer(1, 6),
        HRFlowable(width="30%", thickness=2, color=colors.HexColor(f"#{MINT}"), hAlign="LEFT"),
        Spacer(1, 10),
    ]
    for kind, text in blocks:
        if kind == "bullet":
            flow.append(Paragraph(_inline(text), st["bullet"], bulletText="•"))
        else:
            flow.append(Paragraph(_inline(text), st[kind]))
    doc.build(flow, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def render(markdown: str) -> dict[str, bytes]:
    return {"docx": build_docx(markdown), "pdf": build_pdf(markdown)}
