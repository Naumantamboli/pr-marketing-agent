import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger
from dotenv import load_dotenv

from utils import prompts, storage
from utils.github_client import GitHubClient
from utils.llm import call_structured, model_for
from utils.pdf_report import build_pdf, render_markdown
from utils.schemas import Analysis, GeneratedDraft, Triage, Verification, decide
from utils.template_loader import list_templates, load_template, load_theme

load_dotenv()
logger = setup_logger(__name__)

DIFF_CHAR_BUDGET = int(os.getenv("DIFF_CHAR_BUDGET", "60000"))
MAX_PRS_PER_RUN = int(os.getenv("MAX_PRS_PER_RUN", "30"))


@tool(name="fetch_pr_tool")
async def fetch_pr_tool(pr_url: str) -> dict[str, Any]:
    """Fetch PR metadata, commits, linked issues and a sanitized, size-capped diff."""
    async with GitHubClient() as gh:
        pr = await gh.fetch_pr(pr_url, DIFF_CHAR_BUDGET)
    d = pr["diff"]
    logger.info(
        "Fetched %s#%s: %d patches, %d summarized, %d skipped (%d chars)",
        pr["repo"],
        pr["number"],
        len(d["included"]),
        len(d["summarized"]),
        len(d["skipped"]),
        d["chars"],
    )
    return pr


@tool(name="list_merged_prs_tool")
async def list_merged_prs_tool(repos: list[str], lookback_days: int) -> dict[str, Any]:
    """PRs merged since each repo's saved cursor (or lookback_days on the first run)."""
    now = datetime.now(UTC)
    fallback = now - timedelta(days=lookback_days)
    prs: list[dict[str, Any]] = []
    since_by_repo: dict[str, datetime] = {}
    async with GitHubClient() as gh:
        for repo in repos:
            cursor = storage.read_cursor(repo)
            since = datetime.fromisoformat(cursor.replace("Z", "+00:00")) if cursor else fallback
            since_by_repo[repo] = since
            found = await gh.list_merged_prs(repo, since, MAX_PRS_PER_RUN + 1)
            logger.info("%s: %d PRs merged since %s", repo, len(found), since.isoformat())
            prs += [{**p, "repo": repo} for p in found]
    # Oldest first; anything over the cap is picked up by the next run.
    prs.sort(key=lambda p: p["merged_at"])
    truncated = len(prs) > MAX_PRS_PER_RUN
    prs = prs[:MAX_PRS_PER_RUN]
    cursors = {}
    for repo, since in since_by_repo.items():
        kept = [p["merged_at"] for p in prs if p["repo"] == repo]
        if kept:
            cursors[repo] = kept[-1]
        else:
            cursors[repo] = since.isoformat() if truncated else now.isoformat()
    return {
        "period": {"from": min(since_by_repo.values()).isoformat(), "to": now.isoformat()},
        "prs": prs,
        "truncated": truncated,
        "cursors": cursors,
    }


def select_formats(
    decision: str,
    requested: list[str],
    recommended: list[str],
    force: bool,
    use_recommended: bool,
) -> tuple[list[str], list[dict[str, str]]]:
    """Which per-PR drafts to write for this verdict, and which were skipped and why."""
    wanted = requested or (recommended if use_recommended else [])
    # Multi-PR templates (the digest) are only written by scheduled runs.
    wanted = [f for f in dict.fromkeys(wanted) if load_template(f)["scope"] != "multi"]
    if force or decision == "post_worthy":
        return wanted, []
    if decision == "release_notes_only":
        keep = [f for f in wanted if load_template(f)["channel"] != "social"]
        reason = "Not strong enough for social posts; release notes / owned channels only."
        return keep, [{"format": f, "reason": reason} for f in wanted if f not in keep]
    reason = (
        "On hold: not merged, draft, or behind a feature flag."
        if decision == "hold"
        else "Internal change; no customer-facing content."
    )
    return [], [{"format": f, "reason": reason} for f in wanted]


@tool(name="triage_tool")
async def triage_tool(
    pr: dict[str, Any], requested_formats: list[str], force: bool, use_recommended: bool
) -> dict[str, Any]:
    """Cheap relevance screen; the verdict and the formats to write are decided in code."""
    known = set(list_templates())
    unknown = [f for f in requested_formats if f not in known]
    if unknown:
        raise ValueError(f"Unknown format(s) {unknown}. Available: {sorted(known)}")
    triage = await call_structured(
        "triage", prompts.triage_prompt(pr, list_templates()), Triage, f"triage:{pr['url']}"
    )
    decision = decide(triage, merged=pr["merged"], draft=pr["draft"])
    verdict = {
        "decision": decision,
        **triage.model_dump(),
        "recommended_formats": [f for f in triage.recommended_formats if f in known],
    }
    if pr["draft"] or not pr["merged"]:
        verdict["reasons"].append("PR is not merged yet" + (" (draft)" if pr["draft"] else ""))
    verdict["formats_to_generate"], verdict["skipped_formats"] = select_formats(
        decision, requested_formats, verdict["recommended_formats"], force, use_recommended
    )
    logger.info("Triage %s -> %s (%d)", pr["url"], decision, triage.score)
    return verdict


@tool(name="analyze_tool")
async def analyze_tool(pr: dict[str, Any], extra_context: str | None) -> dict[str, Any]:
    """Plain-English explanation with verified facts separated from assumptions."""
    analysis = await call_structured(
        "analysis", prompts.analysis_prompt(pr, extra_context), Analysis, f"analysis:{pr['url']}"
    )
    return analysis.model_dump()


@tool(name="generate_content_tool")
async def generate_content_tool(
    fmt: str,
    theme_name: str | None,
    sources: list[dict[str, Any]],
    extra_context: str | None,
    period: str | None,
) -> dict[str, Any]:
    """Fill one template from the given PR analyses in the chosen brand theme."""
    template = load_template(fmt)
    theme = load_theme(theme_name)
    step = "longform" if template["model"] == "longform" else "content"
    draft = await call_structured(
        step,
        prompts.content_prompt(template, theme, sources, extra_context, period),
        GeneratedDraft,
        f"content:{fmt}",
    )
    return {
        "format": fmt,
        "theme": theme["name"],
        "title": draft.title,
        "content": draft.content,
        "char_count": len(draft.content),
        "model": model_for(step),
        "claims_checked": False,
        "flags": [],
        "warnings": [],
        "status": "draft_needs_approval",
    }


@tool(name="verify_draft_tool")
async def verify_draft_tool(draft: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Strip any claim in the draft that isn't backed by a verified fact."""
    check = await call_structured(
        "verify",
        prompts.verify_prompt(draft["content"], sources),
        Verification,
        f"verify:{draft['format']}",
    )
    content = draft["content"]
    if not check.all_claims_supported and check.revised_content:
        content = check.revised_content
    warnings = []
    max_chars = load_template(draft["format"])["meta"].get("max_chars")
    if max_chars and len(content) > max_chars:
        warnings.append(
            f"Over the {max_chars}-character limit ({len(content)}); trim before posting."
        )
    return {
        **draft,
        "content": content,
        "char_count": len(content),
        "claims_checked": True,
        "flags": [f"{c.claim} ({c.reason})" for c in check.unsupported_claims],
        "warnings": warnings,
    }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "report"


@tool(name="render_report_tool")
async def render_report_tool(result: dict[str, Any]) -> dict[str, Any]:
    """Build the Markdown + PDF report and store both; returns download links."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if result.get("mode") == "scheduled":
        name = f"product-update-{result['period']['to'][:10]}"
    else:
        pr = result["items"][0]["pr"]
        name = f"{_slug(pr['repo'])}-pr-{pr['number']}"
    base = f"{storage.PREFIX}/reports/{name}-{stamp}"
    pdf_key = storage.put(f"{base}.pdf", build_pdf(result), "application/pdf")
    md_key = storage.put(f"{base}.md", render_markdown(result).encode(), "text/markdown")
    return {
        "pdf": {
            "type": "s3_download_link",
            "title": f"{name}.pdf",
            "file_key": pdf_key,
            "label": "Download marketing report (PDF)",
            "extension": "pdf",
        },
        "markdown": {
            "type": "s3_download_link",
            "title": f"{name}.md",
            "file_key": md_key,
            "label": "Download drafts (Markdown)",
            "extension": "md",
        },
    }


@tool(name="save_cursors_tool")
async def save_cursors_tool(cursors: dict[str, str]) -> None:
    """Advance scheduled-run cursors; called only after a run succeeds."""
    for repo, last in cursors.items():
        storage.write_cursor(repo, last, {"saved_at": datetime.now(UTC).isoformat()})
