import pytest

from utils import doc_sections
from utils.doc_sections import extract_sections

TEMPLATE_PAGE = """# Export v2

## Overview
Export any report as CSV or XLSX.

## User value
Finance teams stop copy-pasting into spreadsheets.

### Use cases
Month-end close.

## How it works
A background job builds the file.

## Limitations
Max 100k rows.

## Availability
Pro and Enterprise plans, all regions, from 1 November. Behind the `export_v2` flag until then.
"""


def test_template_page():
    s = extract_sections(TEMPLATE_PAGE)
    assert s["overview"]["text"] == "Export any report as CSV or XLSX."
    # Subsections are part of the section, and not counted twice.
    assert s["user_value"]["text"] == (
        "Finance teams stop copy-pasting into spreadsheets.\n\n### Use cases\nMonth-end close."
    )
    assert s["user_value"]["headings"] == ["User value"]
    assert s["limitations"]["text"] == "Max 100k rows."
    assert "export_v2" in s["availability"]["text"]
    assert all(v["method"] == "heading" for v in s.values())
    # "How it works" isn't one of the sections.
    assert "background job" not in str(s)


@pytest.mark.parametrize(
    ("heading", "key"),
    [
        ("## 1. Summary", "overview"),
        ("### Background:", "overview"),
        ("## Why we built this", "user_value"),
        ("## Problem statement", "user_value"),
        ("## Goals", "user_value"),
        ("## Known issues", "limitations"),
        ("## Out-of-scope", "limitations"),
        ("## Non-goals", "limitations"),
        ("## Rollout plan", "availability"),
        ("## Feature flags", "availability"),
        ("## Plans and pricing", "availability"),
        ("**Overview**", "overview"),
        ("**Limitations:**", "limitations"),
        ("## **Availability**", "availability"),
    ],
)
def test_heading_variants(heading, key):
    s = extract_sections(f"{heading}\nSome text.\n")
    assert s[key]["text"] == "Some text."


@pytest.mark.parametrize(
    "heading",
    ["## Test plan", "## Migration plan", "## Background jobs", "## Available endpoints"],
)
def test_common_engineering_headings_are_not_sections(heading):
    s = extract_sections(f"{heading}\nSome text.\n")
    assert all(v["method"] == "not_found" for v in s.values())


@pytest.mark.parametrize(
    ("heading", "key"),
    [
        ("## Plans", "availability"),
        ("## Regions", "availability"),
        ("## Available on", "availability"),
        ("## Supported plans", "availability"),
        ("## Background", "overview"),
    ],
)
def test_ambiguous_words_count_at_start_of_heading(heading, key):
    assert extract_sections(f"{heading}\nSome text.\n")[key]["text"] == "Some text."


def test_non_goals_are_not_user_value():
    s = extract_sections("## Non-goals\nNo PDF export.\n")
    assert s["user_value"]["method"] == "not_found"
    assert s["limitations"]["text"] == "No PDF export."


def test_combined_heading_feeds_both_sections():
    s = extract_sections("## Availability and limitations\nPro only; max 100k rows.\n")
    assert s["availability"]["text"] == s["limitations"]["text"] == "Pro only; max 100k rows."


def test_several_headings_feed_one_section_in_order():
    page = "## Limitations\nMax 100k rows.\n\n## Setup\nx\n\n## Known issues\nSlow on Safari.\n"
    s = extract_sections(page)
    assert s["limitations"]["text"] == "Max 100k rows.\n\nSlow on Safari."
    assert s["limitations"]["headings"] == ["Limitations", "Known issues"]


def test_missing_and_empty_sections_are_not_found():
    s = extract_sections("## Overview\n\n## Limitations\nNone known.\n")
    assert s["overview"] == {
        "text": None,
        "method": "not_found",
        "headings": [],
        "truncated": False,
    }
    assert s["user_value"]["method"] == "not_found"
    assert s["limitations"]["text"] == "None known."


def test_headings_inside_code_blocks_are_ignored():
    page = "## Setup\n```\n## Overview\nnot a heading\n```\n"
    assert extract_sections(page)["overview"]["method"] == "not_found"


def test_section_ends_at_same_or_higher_level_only():
    page = "## Overview\nIntro.\n### Details\nMore.\n## Setup\nSteps.\n"
    assert extract_sections(page)["overview"]["text"] == "Intro.\n### Details\nMore."


def test_long_section_is_truncated(monkeypatch):
    monkeypatch.setattr(doc_sections, "SECTION_CHARS", 10)
    s = extract_sections("## Overview\n" + "x" * 50)
    assert s["overview"]["truncated"]
    assert s["overview"]["text"].startswith("x" * 10)
    assert s["overview"]["text"].endswith("[section truncated]")


def test_empty_page():
    assert all(v["method"] == "not_found" for v in extract_sections("").values())
