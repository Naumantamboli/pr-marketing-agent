"""Pull the marketing-relevant sections out of a feature page (Markdown) by heading.

No LLM here: a section is found only when a heading names it. Sections without a matching
heading come back as "not_found" for the extraction tool to fill by quoting, or leave empty.
"""

import os
import re
from typing import Any

SECTION_CHARS = int(os.getenv("DOC_SECTION_CHARS", "4000"))

# Matched against the normalized heading (lowercase, hyphens as spaces, numbering stripped).
# Words that are common in other headings ("Background jobs", "Test plan", "Available
# endpoints") only count when they start the heading.
SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "overview": re.compile(
        r"\b(overview|summary|introduction|what is it)\b"
        r"|^(background|description|about)\b(?! jobs?)"
    ),
    "user_value": re.compile(
        r"\b(user value|customer value|value proposition|benefits?|why|problem( statement)?"
        r"|use cases?|customer impact|motivation|(?<!non )goals?)\b"
    ),
    "limitations": re.compile(
        r"\b(limitations?|known issues|caveats?|constraints?|out of scope|non goals?"
        r"|restrictions?)\b"
    ),
    "availability": re.compile(
        r"\b(availability|rollout|roll out|release (plan|date|timeline)|launch (plan|date)"
        r"|feature flags?|pricing|eligibility|supported (plans|regions))\b"
        r"|^(plans?|regions?|who can use)\b|^available( (on|in|to|for)\b|$)"
    ),
}

_ATX_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.*?)\s*#*\s*$")
# A line that is only bold text, "**Overview**" or "**Overview:**", used as a heading.
_BOLD_RE = re.compile(r"^\s*(\*\*|__)(?P<text>[^*_]+?)\s*:?\s*\1\s*:?\s*$")
_BOLD_LEVEL = 7  # below every Markdown heading level
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _normalize(heading: str) -> str:
    text = re.sub(r"[*_`]", "", heading).lower().replace("-", " ")
    text = re.sub(r"^\s*(\d+(\.\d+)*\.?|[a-z]\))\s+", "", text)  # "2.1 Overview", "a) Why"
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, text) for each heading outside fenced code blocks."""
    found = []
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if m := _ATX_RE.match(line):
            found.append((i, len(m["hashes"]), m["text"]))
        elif m := _BOLD_RE.match(line):
            found.append((i, _BOLD_LEVEL, m["text"]))
    return found


def extract_sections(markdown: str) -> dict[str, dict[str, Any]]:
    """Text under the headings that name each section, in document order.

    A section runs from its heading to the next heading at the same or a higher level, so
    subsections are included. A heading can feed several sections ("Availability and
    limitations"); several headings can feed one ("Limitations", "Known issues").
    """
    lines = (markdown or "").splitlines()
    heads = _headings(lines)
    spans: dict[str, list[tuple[int, int, str]]] = {key: [] for key in SECTION_PATTERNS}
    for n, (start, level, text) in enumerate(heads):
        end = next((i for i, lv, _ in heads[n + 1 :] if lv <= level), len(lines))
        norm = _normalize(text)
        for key, pattern in SECTION_PATTERNS.items():
            # Skip a subheading already inside a span taken for this section.
            inside = any(s < start < e for s, e, _ in spans[key])
            if pattern.search(norm) and not inside:
                spans[key].append((start, end, text.strip()))

    result: dict[str, dict[str, Any]] = {}
    for key, key_spans in spans.items():
        parts = ["\n".join(lines[s + 1 : e]).strip() for s, e, _ in key_spans]
        body = "\n\n".join(p for p in parts if p)
        if not body:
            result[key] = {"text": None, "method": "not_found", "headings": [], "truncated": False}
            continue
        truncated = len(body) > SECTION_CHARS
        result[key] = {
            "text": body[:SECTION_CHARS] + ("\n… [section truncated]" if truncated else ""),
            "method": "heading",
            "headings": [h for (s, e, h), p in zip(key_spans, parts, strict=True) if p],
            "truncated": truncated,
        }
    return result
