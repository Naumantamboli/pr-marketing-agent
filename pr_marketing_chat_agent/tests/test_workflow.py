from typing import Any

import pytest
from temporalio.exceptions import ActivityError, ApplicationError

from utils.tool_specs import tool_specs


def _call(name: str, args: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {"id": call_id, "name": name, "args": args}


def _activity_error(message: str) -> ActivityError:
    try:
        raise ActivityError(
            "Activity task failed",
            scheduled_event_id=1,
            started_event_id=2,
            identity="w",
            activity_type="x",
            activity_id="1",
            retry_state=None,
        ) from ApplicationError(message, non_retryable=True)
    except ActivityError as err:
        return err


class FakeExecutor:
    """Scripted toolExecutor: decisions in order, activity results by name."""

    def __init__(self, decisions, results=None, loaded=None):
        self.decisions = list(decisions)
        self.results = results or {}
        self.loaded = loaded or {"turns": [], "drafts": {}, "next_draft": 1}
        self.calls: list[tuple[str, Any]] = []
        self.saved: dict[str, Any] | None = None

    async def execute(self, name: str, payload: Any, **options: Any) -> Any:
        assert "start_to_close_timeout" in options, f"{name} called without a timeout"
        self.calls.append((name, payload))
        if name == "load_conversation_tool":
            if isinstance(self.loaded, Exception):
                raise self.loaded
            return self.loaded
        if name == "save_conversation_tool":
            self.saved = payload
            return {}
        if name == "chat_decide_tool":
            d = self.decisions.pop(0)
            if isinstance(d, Exception):
                raise d
            if isinstance(d, str):
                return {"content": d, "tool_calls": []}
            return {"content": "", "tool_calls": d}
        result = self.results[name]
        if isinstance(result, Exception):
            raise result
        return result(payload) if callable(result) else result


@pytest.fixture
def run(wf, monkeypatch):
    async def _run(executor: FakeExecutor, message: str = "What's new?", **payload: Any):
        monkeypatch.setattr(wf.toolExecutor, "execute", executor.execute)
        return await wf.run_turn(
            {
                "requirement": message,
                "task_metadata": {"conversation_id": "conv/1", "customer_id": "cust"},
                **payload,
            }
        )

    return _run


def _tool_messages(ex: FakeExecutor) -> list[dict[str, Any]]:
    decide_payloads = [p for n, p in ex.calls if n == "chat_decide_tool"]
    return [m for m in decide_payloads[-1]["messages"] if m["role"] == "tool"]


def test_tool_specs_match_the_allow_list(wf):
    assert sorted(t["function"]["name"] for t in tool_specs()) == sorted(wf.TOOL_ACTIVITIES)


def test_registered_as_direct_chat_agent(wf):
    meta = wf.PrMarketingChatAgent._aetherion_register_metadata
    assert meta.name == wf.NAME
    assert meta.chat_enabled is True
    assert meta.runtime_config.get("chat_direct") is True
    assert [i["name"] for i in meta.input_schema] == ["requirement"]


async def test_plain_reply_is_returned_as_markdown_and_saved(run):
    ex = FakeExecutor(["Here's what's new."])
    result = await run(ex)
    assert result == "Here's what's new."
    assert ex.saved["conversation_id"] == "conv/1"
    assert ex.saved["customer_id"] == "cust"
    assert ex.saved["turn"]["user"] == "What's new?"
    assert ex.saved["turn"]["reply"] == "Here's what's new."


async def test_history_and_uploads_reach_the_model(run):
    loaded = {"turns": [{"user": "hi", "reply": "hello"}], "drafts": {}, "next_draft": 1}
    ex = FakeExecutor(["ok"], loaded=loaded)
    await run(ex, "Next question", uploaded_files=["kb/file.pdf"])
    messages = ex.calls[1][1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert "can't be read yet" in messages[-1]["content"]
    assert ex.saved["turn"]["uploads"] == ["kb/file.pdf"]


async def test_tool_results_feed_back_and_sources_are_recorded(run):
    ex = FakeExecutor(
        [[_call("read_doc", {"path": "content/docs/chat-agents.mdx"})], "Chat agents are…"],
        results={"docs_read_tool": {"path": "content/docs/chat-agents.mdx", "content": "Text"}},
    )
    result = await run(ex)
    assert result == "Chat agents are…"
    (tool_msg,) = _tool_messages(ex)
    assert tool_msg["tool_call_id"] == "c1"
    assert "Text" in tool_msg["content"]
    assert ex.saved["turn"]["source_refs"] == ["content/docs/chat-agents.mdx"]


async def test_failures_go_back_to_the_model_not_the_user(run):
    ex = FakeExecutor(
        [
            [
                _call("delete_everything", {}, "a"),
                _call("read_doc", {"path": "x"}, "b"),
                _call("jira_issue", {"_invalid_arguments": "{oops"}, "c"),
                _call("find_jira_work", {}, "d"),
            ],
            "Recovered.",
        ],
        results={
            "docs_read_tool": _activity_error("'x' is not a docs page"),
            "jira_find_tool": RuntimeError("boom"),
        },
    )
    assert await run(ex) == "Recovered."
    contents = {m["tool_call_id"]: m["content"] for m in _tool_messages(ex)}
    assert "Unknown tool" in contents["a"]
    assert "is not a docs page" in contents["b"]
    assert "not valid JSON" in contents["c"]
    assert "boom" in contents["d"]
    assert all(n != "jira_issue_tool" for n, _ in ex.calls)


async def test_step_cap_forces_a_text_answer(run, wf):
    loop = [[_call("list_docs", {}, f"c{i}")] for i in range(wf.MAX_STEPS)]
    ex = FakeExecutor([*loop, "Final."], results={"docs_index_tool": {"pages": []}})
    assert await run(ex) == "Final."
    last = [p for n, p in ex.calls if n == "chat_decide_tool"][-1]
    assert last["allow_tools"] is False


async def test_time_budget_forces_a_text_answer(run, wf):
    def slow(_payload):
        wf.clock.advance(9)
        return {"pages": []}

    ex = FakeExecutor([[_call("list_docs", {})], "Final."], results={"docs_index_tool": slow})
    assert await run(ex) == "Final."
    decides = [p for n, p in ex.calls if n == "chat_decide_tool"]
    assert [p["allow_tools"] for p in decides] == [True, False]


async def test_drafts_files_and_artifacts_shape(run):
    draft = {"format": "linkedin_post", "title": "T", "content": "Body", "source_refs": ["AP-1"]}
    link = {
        "type": "s3_download_link",
        "file_key": "k.png",
        "title": "d1 image",
        "extension": "png",
    }
    ex = FakeExecutor(
        [
            [
                _call(
                    "write_draft",
                    {"format": "linkedin_post", "topic": "x", "source_refs": ["AP-1"]},
                )
            ],
            [_call("make_social_visual", {"draft_id": "d1", "layout": "headline_dark"})],
            "Here you go.",
        ],
        results={
            "write_draft_tool": draft,
            "render_social_tool": lambda p: {"layout": p["layout"], "copy": {}, "files": [link]},
        },
    )
    result = await run(ex)
    assert result == {"artifacts": [link, {"title": "Details", "text": "Here you go."}]}
    render_payload = next(p for n, p in ex.calls if n == "render_social_tool")
    assert render_payload["draft"]["content"] == "Body"
    assert render_payload["customer_id"] == "cust"
    assert render_payload["key_base"] == "pr_marketing_chat/conv_1/run-1-2-d1"
    assert list(ex.saved["drafts"]) == ["d1"]
    assert ex.saved["turn"]["files"] == [{"title": "d1 image", "file_key": "k.png"}]
    assert ex.saved["turn"]["source_refs"] == ["AP-1"]


async def test_revision_gets_the_stored_draft(run):
    loaded = {
        "turns": [],
        "drafts": {"d3": {"id": "d3", "format": "x_post", "content": "Old", "title": None}},
        "next_draft": 4,
    }
    ex = FakeExecutor(
        [
            [_call("write_draft", {"format": "x_post", "topic": "t", "base_draft_id": "D3"})],
            [_call("make_blog_document", {"draft_id": "d9"}, "c2")],
            "Done.",
        ],
        results={"write_draft_tool": {"format": "x_post", "content": "New", "source_refs": []}},
        loaded=loaded,
    )
    await run(ex)
    payload = next(p for n, p in ex.calls if n == "write_draft_tool")
    assert payload["base_draft"]["content"] == "Old"
    assert list(ex.saved["drafts"]) == ["d4"]
    assert "No draft 'd9'" in _tool_messages(ex)[-1]["content"]


async def test_a_broken_turn_still_answers_and_saves(run, wf):
    ex = FakeExecutor([RuntimeError("gateway down")], loaded=RuntimeError("storage down"))
    assert await run(ex) == wf.ERROR_REPLY
    assert ex.saved["turn"]["reply"] == wf.ERROR_REPLY


async def test_empty_message_gets_a_greeting(run):
    ex = FakeExecutor([])
    result = await run(ex, "   ")
    assert result.startswith("Hi!")
    assert ex.calls == []
