import pytest

from tools import activities
from utils.schemas import GeneratedDraft, SlotCopy, UnsupportedClaim, Verification

SOURCES = [
    {"ref": "content/docs/connectors.mdx", "title": "Connectors", "text": "Connect Jira from Apps."}
]


@pytest.fixture
def fake_llm(monkeypatch):
    """Scripted `structured` answers by schema."""
    answers: dict[type, list] = {}
    prompts: list[str] = []

    async def structured(step, prompt, schema):
        prompts.append(prompt)
        return answers[schema].pop(0)

    monkeypatch.setattr(activities, "structured", structured)
    answers["prompts"] = prompts
    return answers


@pytest.fixture
def fake_sources(monkeypatch):
    seen: list[list[str]] = []

    async def fetch(refs):
        seen.append(refs)
        return SOURCES, []

    monkeypatch.setattr(activities, "_fetch_sources", fetch)
    return seen


async def test_write_draft_removes_unsupported_claims(fake_llm, fake_sources):
    fake_llm[GeneratedDraft] = [GeneratedDraft(title="T", content="Jira in Apps. 10x faster!")]
    fake_llm[Verification] = [
        Verification(
            all_claims_supported=False,
            unsupported_claims=[UnsupportedClaim(claim="10x faster", reason="not in sources")],
            revised_content="Jira in Apps.",
        )
    ]
    draft = await activities.write_draft_tool(
        {
            "format": "x_post",
            "topic": "Jira",
            "source_refs": ["content/docs/connectors.mdx", "content/docs/connectors.mdx"],
        }
    )
    assert draft["content"] == "Jira in Apps."
    assert draft["removed_claims"] == ["10x faster (not in sources)"]
    assert draft["theme"] == "calfus" and draft["source_refs"] == ["content/docs/connectors.mdx"]
    assert fake_sources == [["content/docs/connectors.mdx"]]
    assert "Connect Jira from Apps." in fake_llm["prompts"][0]  # drafted from the real text
    assert "Connect Jira from Apps." in fake_llm["prompts"][1]  # and checked against it


async def test_revision_reuses_base_sources_and_text(fake_llm, fake_sources):
    fake_llm[GeneratedDraft] = [GeneratedDraft(content="Short.")]
    fake_llm[Verification] = [Verification(all_claims_supported=True)]
    base = {
        "id": "d1",
        "format": "linkedin_post",
        "topic": "Jira",
        "content": "Long draft",
        "source_refs": ["AP-1"],
    }
    draft = await activities.write_draft_tool(
        {"base_draft": base, "instructions": "make it shorter", "topic": ""}
    )
    assert draft["format"] == "linkedin_post" and draft["content"] == "Short."
    assert fake_sources == [["AP-1"]]
    assert "Long draft" in fake_llm["prompts"][0] and "make it shorter" in fake_llm["prompts"][0]


async def test_write_draft_needs_sources():
    with pytest.raises(ValueError, match="source_refs"):
        await activities.write_draft_tool({"format": "x_post", "topic": "t"})


async def test_render_social_retries_until_copy_fits(fake_llm, local_output):
    fake_llm[SlotCopy] = [
        SlotCopy(
            slots={"accent": "Way too long for the accent slot", "headline": "h", "body": "b"}
        ),
        SlotCopy(slots={"accent": "Jira", "headline": "now in Apps", "body": "Connect once."}),
    ]
    out = await activities.render_social_tool(
        {
            "draft": {"id": "d1", "content": "Connect Jira from Apps.", "title": "T"},
            "layout": "headline_light",
            "key_base": "pr_marketing_chat/c/run-1-1-d1",
        }
    )
    assert "did not fit" in fake_llm["prompts"][1]
    assert [f["extension"] for f in out["files"]] == ["png", "pptx"]
    assert (local_output / "pr_marketing_chat/c/run-1-1-d1-headline_light.png").is_file()
    assert out["files"][0]["type"] == "s3_download_link"


async def test_render_blog_writes_docx_and_pdf(local_output):
    out = await activities.render_blog_tool(
        {
            "draft": {"id": "d2", "format": "blog_draft", "title": "Jira", "content": "Body text."},
            "key_base": "k/run-1-1-d2",
        }
    )
    assert [f["extension"] for f in out["files"]] == ["docx", "pdf"]
    assert (local_output / "k/run-1-1-d2-blog.pdf").read_bytes()[:4] == b"%PDF"


async def test_conversation_round_trip(local_output):
    ids = {"conversation_id": "conv-1", "customer_id": "cust"}
    first = await activities.load_conversation_tool(ids)
    assert first == {"turns": [], "drafts": {}, "next_draft": 1}
    await activities.save_conversation_tool(
        {
            **ids,
            "turn": {"user": "hi", "reply": "hello"},
            "drafts": {"d1": {"id": "d1", "format": "x_post", "content": "c"}},
        }
    )
    again = await activities.load_conversation_tool(ids)
    assert again["turns"] == [{"user": "hi", "reply": "hello"}]
    assert again["next_draft"] == 2


async def test_decide_builds_system_prompt_and_tools(monkeypatch):
    seen = {}

    async def decide(system, messages, tools):
        seen.update(system=system, tools=tools)
        return {"content": "ok", "tool_calls": []}

    monkeypatch.setattr(activities, "decide", decide)
    drafts = {"d1": {"id": "d1", "format": "x_post", "title": "Hello", "content": "Draft text"}}
    await activities.chat_decide_tool({"messages": [], "drafts": drafts})
    assert 'd1: x_post "Hello"' in seen["system"] and len(seen["tools"]) == 8
    await activities.chat_decide_tool({"messages": [], "allow_tools": False})
    assert seen["tools"] is None
