"""Activities for PrMarketingChatAgent. Each takes and returns one JSON-serialisable dict.

Bad input raises ValueError (not retried); the workflow hands the message back to the model so
it can correct itself. Keep @tool timeouts at 5 minutes or less; the workflow passes longer
limits per call where it needs them.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger
from dotenv import load_dotenv

from brand import blog, social
from utils import conversation, prompts, storage
from utils.docs_client import DocsClient
from utils.jira_client import JiraClient
from utils.llm import decide, structured
from utils.schemas import GeneratedDraft, SlotCopy, Verification
from utils.template_loader import list_templates, load_template, load_theme
from utils.text import clip
from utils.tool_specs import tool_specs

load_dotenv()
logger = setup_logger(__name__)

_FIVE_MIN = timedelta(minutes=5)
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
MAX_SOURCES = 8
SOURCE_CHARS = 40000


@tool(name="load_conversation_tool", start_to_close_timeout=timedelta(minutes=1))
async def load_conversation_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Recent turns and drafts for this conversation (empty for a new one)."""
    bucket = storage.bucket_for(payload.get("customer_id"))
    cid = payload["conversation_id"]
    state = storage.get_json(bucket, conversation.key(cid)) or conversation.empty(cid)
    return conversation.for_turn(state)


@tool(name="save_conversation_tool", start_to_close_timeout=timedelta(minutes=1))
async def save_conversation_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Append one finished turn (and its drafts) to the stored conversation."""
    bucket = storage.bucket_for(payload.get("customer_id"))
    cid = payload["conversation_id"]
    stored = storage.get_json(bucket, conversation.key(cid)) or conversation.empty(cid)
    state = conversation.append(stored, payload["turn"], payload.get("drafts") or {})
    storage.put_json(bucket, conversation.key(cid), state)
    return {"turns": len(state["turns"]), "drafts": len(state["drafts"])}


def _formats() -> dict[str, str]:
    return {f: load_template(f)["meta"].get("channel", "owned") for f in list_templates()}


@tool(name="chat_decide_tool", start_to_close_timeout=_FIVE_MIN)
async def chat_decide_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """One model step: a reply, or tool calls to make first."""
    system = prompts.system_prompt(
        datetime.now(UTC).strftime("%A %d %B %Y"),
        _formats(),
        social.layout_guide(),
        conversation.draft_catalog(payload.get("drafts") or {}),
    )
    tools = tool_specs() if payload.get("allow_tools", True) else None
    return await decide(system, payload["messages"], tools)


@tool(name="docs_index_tool", start_to_close_timeout=_FIVE_MIN)
async def docs_index_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Every docs page in scope: path, title, description."""
    async with DocsClient() as docs:
        _, pages = await docs.index()
    return {"pages": pages}


@tool(name="docs_read_tool", start_to_close_timeout=timedelta(minutes=2))
async def docs_read_tool(payload: dict[str, Any]) -> dict[str, Any]:
    async with DocsClient() as docs:
        return await docs.read(str(payload.get("path") or ""))


@tool(name="docs_changes_tool", start_to_close_timeout=timedelta(minutes=2))
async def docs_changes_tool(payload: dict[str, Any]) -> dict[str, Any]:
    days = max(1, min(int(payload.get("days") or 14), 180))
    async with DocsClient() as docs:
        return {"days": days, "changes": await docs.changes(days)}


@tool(name="jira_find_tool", start_to_close_timeout=timedelta(minutes=2))
async def jira_find_tool(payload: dict[str, Any]) -> dict[str, Any]:
    days = payload.get("days")
    async with JiraClient() as jira:
        return await jira.find(
            text=payload.get("text"),
            days=max(1, min(int(days), 180)) if days else None,
            status=payload.get("status") or "any",
        )


@tool(name="jira_issue_tool", start_to_close_timeout=timedelta(minutes=2))
async def jira_issue_tool(payload: dict[str, Any]) -> dict[str, Any]:
    async with JiraClient() as jira:
        return await jira.issue(str(payload.get("key") or ""))


async def _fetch_sources(refs: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Real text for each ref (docs path or Jira key); the draft is checked against this."""
    docs_refs = [r for r in refs if not _JIRA_KEY_RE.match(r.strip().upper())]
    jira_refs = [r for r in refs if _JIRA_KEY_RE.match(r.strip().upper())]
    sources, errors = [], []
    if docs_refs:
        async with DocsClient() as docs:
            for ref in docs_refs:
                try:
                    page = await docs.read(ref)
                    sources.append({"ref": ref, "title": page["title"], "text": page["content"]})
                except ValueError as err:
                    errors.append(f"{ref}: {err}")
    if jira_refs:
        try:
            async with JiraClient() as jira:
                for ref in jira_refs:
                    try:
                        i = await jira.issue(ref)
                    except ValueError as err:
                        errors.append(f"{ref}: {err}")
                        continue
                    text = (
                        f"{i['summary']}\nType: {i['type']}. Stage: {i['stage']} ({i['status']})."
                        f"\nEpic: {i['epic'] or '-'}\n\n{i['description']}"
                    )
                    sources.append({"ref": ref, "title": i["summary"], "text": text})
        except ValueError as err:
            errors.append(str(err))
    budget = SOURCE_CHARS // max(len(sources), 1)
    return [{**s, "text": clip(s["text"], budget)} for s in sources], errors


@tool(name="write_draft_tool", start_to_close_timeout=_FIVE_MIN)
async def write_draft_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Template + theme + sources -> draft, then a claim check against the same sources."""
    base = payload.get("base_draft")
    refs = [str(r) for r in payload.get("source_refs") or []] or (base or {}).get("source_refs", [])
    refs = list(dict.fromkeys(r.strip() for r in refs if r.strip()))[:MAX_SOURCES]
    if not refs:
        raise ValueError("write_draft needs source_refs: the docs paths or Jira keys you read.")
    fmt = payload.get("format") or (base or {}).get("format")
    template = load_template(fmt)
    theme = load_theme(payload.get("theme") or (base or {}).get("theme"))
    sources, errors = await _fetch_sources(refs)
    if not sources:
        raise ValueError("None of the sources could be read: " + "; ".join(errors))
    step = "longform" if template["model"] == "longform" else "content"
    draft = await structured(
        step,
        prompts.draft_prompt(
            template,
            theme,
            payload.get("topic") or (base or {}).get("topic", ""),
            sources,
            payload.get("audience"),
            payload.get("notes"),
            base,
            payload.get("instructions"),
        ),
        GeneratedDraft,
    )
    check = await structured("verify", prompts.verify_prompt(draft.content, sources), Verification)
    content = draft.content
    if not check.all_claims_supported and check.revised_content:
        content = check.revised_content
    warnings = [f"Couldn't read source {e}" for e in errors]
    max_chars = template["meta"].get("max_chars")
    if max_chars and len(content) > max_chars:
        warnings.append(f"Over the {max_chars}-character limit ({len(content)}); trim it.")
    logger.info(
        "Draft %s from %d source(s), %d claim(s) removed",
        fmt,
        len(sources),
        len(check.unsupported_claims),
    )
    return {
        "format": fmt,
        "theme": theme["name"],
        "topic": payload.get("topic") or (base or {}).get("topic", ""),
        "title": draft.title,
        "content": content,
        "source_refs": [s["ref"] for s in sources],
        "removed_claims": [f"{c.claim} ({c.reason})" for c in check.unsupported_claims],
        "warnings": warnings,
    }


def _link(bucket: str, key: str, data: bytes, ext: str, title: str, mime: str) -> dict[str, Any]:
    storage.put(bucket, key, data, mime)
    return {
        "type": "s3_download_link",
        "file_key": key,
        "title": title,
        "label": f"Download {title}",
        "extension": ext,
    }


@tool(name="render_social_tool", start_to_close_timeout=_FIVE_MIN)
async def render_social_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Draft -> slot copy for the layout (only the draft's wording) -> .png + .pptx."""
    draft, layout = payload["draft"], payload["layout"]
    slots = social.layouts().get(layout, {}).get("slots")
    if not slots:
        raise ValueError(f"Unknown layout {layout!r}. Available: {', '.join(social.layouts())}")
    problem, copy = None, None
    for _ in range(3):
        answer = await structured(
            "content", prompts.slot_copy_prompt(draft, layout, slots, problem), SlotCopy
        )
        try:
            files = social.render(layout, answer.slots)
            copy = answer.slots
            break
        except ValueError as err:
            problem = str(err)
    if copy is None:
        raise ValueError(f"Couldn't fit the copy into {layout}: {problem}")
    bucket, base = storage.bucket_for(payload.get("customer_id")), payload["key_base"]
    name = f"{draft['id']} {layout.replace('_', ' ')}"
    return {
        "layout": layout,
        "copy": copy,
        "files": [
            _link(
                bucket, f"{base}-{layout}.png", files["png"], "png", f"{name} image", "image/png"
            ),
            _link(
                bucket,
                f"{base}-{layout}.pptx",
                files["pptx"],
                "pptx",
                f"{name} (editable)",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ),
        ],
    }


@tool(name="render_blog_tool", start_to_close_timeout=timedelta(minutes=2))
async def render_blog_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Draft markdown -> branded .docx + .pdf."""
    draft = payload["draft"]
    markdown = draft["content"]
    if draft.get("title") and not markdown.lstrip().startswith("# "):
        markdown = f"# {draft['title']}\n\n{markdown}"
    files = blog.render(markdown)
    bucket, base = storage.bucket_for(payload.get("customer_id")), payload["key_base"]
    name = f"{draft['id']} {draft.get('title') or draft['format']}"[:60]
    return {
        "files": [
            _link(
                bucket,
                f"{base}-blog.docx",
                files["docx"],
                "docx",
                f"{name} (Word)",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            _link(
                bucket, f"{base}-blog.pdf", files["pdf"], "pdf", f"{name} (PDF)", "application/pdf"
            ),
        ]
    }
