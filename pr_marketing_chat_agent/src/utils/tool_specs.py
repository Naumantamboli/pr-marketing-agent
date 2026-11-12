"""Function schemas the chat model sees. The workflow maps each name to an activity
(workflow.TOOL_ACTIVITIES); anything not listed there is refused."""

from typing import Any

from brand.social import layouts
from utils.template_loader import list_templates, list_themes


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def tool_specs() -> list[dict[str, Any]]:
    days = {"type": "integer", "minimum": 1, "maximum": 180, "description": "Look back N days"}
    return [
        _fn(
            "product_catalog",
            "START HERE for any product question. The reviewed product catalog: what Aetherion "
            "is, how it is built (architecture and components), and every feature with its "
            "stage, one-line summary and aliases.",
            {},
            [],
        ),
        _fn(
            "catalog_feature",
            "One feature's full catalog record: what it is, who it's for, how it works, "
            "limitations, recent dated updates, components, and the sources behind them.",
            {"feature": {"type": "string", "description": "Feature slug, name or alias"}},
            ["feature"],
        ),
        _fn(
            "catalog_changes",
            "Dated product changes recorded in the catalog across all features and the "
            "architecture, newest first. Use for 'what's new / what changed lately'.",
            {"days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30}},
            [],
        ),
        _fn(
            "list_docs",
            "List every page of the product docs (path, title, description). Use it to find "
            "which pages to read.",
            {},
            [],
        ),
        _fn(
            "read_doc",
            "Read one docs page by its path from list_docs.",
            {"path": {"type": "string"}},
            ["path"],
        ),
        _fn(
            "whats_new_in_docs",
            "Recent changes to the product docs repo: commit messages and the pages they "
            "touched. Use when catalog_changes doesn't cover what you need.",
            {"days": {**days, "default": 14}},
            [],
        ),
        _fn(
            "find_jira_work",
            "Find Jira work items in the product projects. All filters optional.",
            {
                "text": {"type": "string", "description": "Words to search for"},
                "days": {**days, "description": "Done in / updated in the last N days"},
                "status": {
                    "type": "string",
                    "enum": ["any", "done", "in_progress", "planned"],
                    "default": "any",
                },
            },
            [],
        ),
        _fn(
            "jira_issue",
            "Details of one Jira item (summary, status, epic, description).",
            {"key": {"type": "string", "description": "Issue key like AP-123"}},
            ["key"],
        ),
        _fn(
            "write_draft",
            "Write a fact-checked marketing draft from sources you have read, or revise an "
            "existing draft (pass base_draft_id + instructions).",
            {
                "format": {"type": "string", "enum": list_templates()},
                "topic": {"type": "string", "description": "What the piece is about"},
                "source_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What the draft is based on: catalog:<feature-slug>, docs page "
                    "paths and/or Jira keys. "
                    "Optional when revising (the base draft's sources are reused).",
                },
                "audience": {"type": "string"},
                "notes": {"type": "string", "description": "Extra direction from the user"},
                "theme": {"type": "string", "enum": list_themes(), "default": "calfus"},
                "base_draft_id": {"type": "string", "description": "e.g. d1"},
                "instructions": {"type": "string", "description": "What to change when revising"},
            },
            ["format", "topic"],
        ),
        _fn(
            "make_social_visual",
            "Turn a draft into a branded 1080x1350 social image (.png) and an editable .pptx.",
            {
                "draft_id": {"type": "string"},
                "layout": {"type": "string", "enum": list(layouts())},
            },
            ["draft_id", "layout"],
        ),
        _fn(
            "make_blog_document",
            "Turn a draft (usually a blog_draft) into a branded Word document and PDF.",
            {"draft_id": {"type": "string"}},
            ["draft_id"],
        ),
    ]
