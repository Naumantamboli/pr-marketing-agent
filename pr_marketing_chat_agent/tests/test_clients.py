import pytest

from utils import docs_client, jira_client
from utils.docs_client import DocsClient, in_scope, index_entry, readable
from utils.jira_client import JiraClient, adf_to_text, build_jql, summarize_issue

MDX = """---
title: Chat Agents
description: Make an agent conversational
---
import { Card } from "x"

A **chat agent** is a normal agent.

<Note>
Old decorators are removed.
</Note>
"""


def test_index_entry_reads_front_matter_then_heading():
    assert index_entry("content/docs/chat-agents.mdx", MDX) == {
        "path": "content/docs/chat-agents.mdx",
        "title": "Chat Agents",
        "description": "Make an agent conversational",
    }
    assert index_entry("kb/a.md", "# Storage Client\n\ntext")["title"] == "Storage Client"
    assert index_entry("kb/b.md", "no heading")["title"] == "b.md"


def test_readable_drops_front_matter_and_mdx_noise():
    text = readable(MDX)
    assert "import" not in text and "<Note>" not in text and "title:" not in text
    assert "A **chat agent** is a normal agent." in text
    assert "Old decorators are removed." in text


def test_scope_is_limited_to_doc_folders():
    roots = ["content/docs"]
    assert in_scope("content/docs/a.mdx", roots)
    assert not in_scope("content/docs-old/a.mdx", roots)
    assert not in_scope("app/page.tsx", roots)
    assert not in_scope("content/docs/meta.json", roots)


async def test_read_refuses_paths_outside_the_index(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")

    async def index(self):
        return "sha", [{"path": "content/docs/a.mdx", "title": "A", "description": ""}]

    async def raw(self, path, ref):
        assert path == "content/docs/a.mdx"
        return "---\ntitle: A\n---\nBody token: abcdefghij"

    monkeypatch.setattr(DocsClient, "index", index)
    monkeypatch.setattr(DocsClient, "_raw", raw)
    async with DocsClient() as docs:
        page = await docs.read("/content/docs/a.mdx")
        with pytest.raises(ValueError, match="not a docs page"):
            await docs.read("../../.github/secrets.md")
    assert page["content"] == "Body token: [REDACTED]"


async def test_changes_lists_doc_commits_newest_first(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")

    def commit(sha, date, msg):
        return {"sha": sha, "commit": {"message": msg, "committer": {"date": date}}}

    async def get(self, url, raw=False, **params):
        if url.endswith("/commits"):
            return [
                commit("1", "2026-10-01T00:00:00Z", "docs: old"),
                commit("2", "2026-10-20T00:00:00Z", "docs: AP-1 new\n\nbody"),
            ]
        return {"files": [{"filename": "content/docs/x.mdx"}, {"filename": "app/y.tsx"}]}

    monkeypatch.setattr(DocsClient, "_get", get)
    async with DocsClient() as docs:
        changes = await docs.changes(30)
    assert [c["message"] for c in changes] == ["docs: AP-1 new", "docs: old"]
    assert changes[0] == {
        "date": "2026-10-20",
        "message": "docs: AP-1 new",
        "pages": ["content/docs/x.mdx"],
    }


def test_docs_client_needs_a_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        DocsClient()
    assert docs_client._settings()[0] == "CalfusAgentFoundry/lore"


def test_jql_is_always_project_scoped_and_sanitized():
    jql = build_jql(["AP"], text='chat") OR project = SECRET OR ("', days=14, status="done")
    assert jql.startswith("project in (AP) AND issuetype not in subTaskIssueTypes()")
    assert 'statusCategory = "Done"' in jql and "resolved >= -14d" in jql
    assert jql.count('"') == 4  # only our own quotes survive
    assert (
        "OR project = SECRET OR" in jql.split('text ~ "')[1].split('"')[0]
    )  # inert inside the phrase
    assert jql.endswith("ORDER BY resolved DESC")
    assert "updated >= -7d" in build_jql(["AP", "UI"], days=7)
    with pytest.raises(ValueError, match="JIRA_PROJECTS"):
        build_jql([], text="x")
    with pytest.raises(ValueError, match="status"):
        build_jql(["AP"], status="closed")


def test_adf_to_text():
    adf = {
        "type": "doc",
        "content": [
            {"type": "heading", "content": [{"type": "text", "text": "Goal"}]},
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Ship it"},
                    {"type": "hardBreak"},
                    {"type": "mention", "attrs": {"text": "@Ana"}},
                ],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}
                        ],
                    }
                ],
            },
        ],
    }
    assert adf_to_text(adf).strip() == "Goal\n\nShip it\n@Ana\n\n- one"


def test_summarize_issue_maps_status_to_stage():
    issue = {
        "key": "AP-7",
        "fields": {
            "summary": "Jira connector",
            "status": {"name": "In Review", "statusCategory": {"key": "indeterminate"}},
            "issuetype": {"name": "Story"},
            "parent": {"key": "AP-1", "fields": {"summary": "Connectors"}},
            "fixVersions": [{"name": "1.4"}],
            "resolutiondate": None,
            "updated": "2026-10-20T10:00:00.000+0000",
        },
    }
    s = summarize_issue(issue)
    assert s["stage"] == "in progress"
    assert s["epic"] == "AP-1: Connectors"
    assert s["fix_versions"] == ["1.4"] and s["updated"] == "2026-10-20"


async def test_issue_refuses_other_projects(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://x.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@x.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    monkeypatch.setenv("JIRA_PROJECTS", "AP")
    async with JiraClient() as jira:
        with pytest.raises(ValueError, match="allowed projects"):
            await jira.issue("HR-1")
        with pytest.raises(ValueError, match="allowed projects"):
            await jira.issue("AP-1/../../rest")


def test_jira_client_needs_config(monkeypatch):
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError, match="Jira isn't configured"):
        JiraClient()
    assert jira_client.allowed_projects() == [] or isinstance(jira_client.allowed_projects(), list)
