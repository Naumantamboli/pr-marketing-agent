from utils import conversation
from utils.llm import flatten_tool_turns, model_for, normalize_reply


def _draft(i: str, content: str = "x") -> dict:
    return {"id": i, "format": "x_post", "title": None, "content": content}


def test_append_keeps_turns_from_overlapping_runs():
    stored = conversation.empty("c")
    stored = conversation.append(stored, {"user": "a"}, {"d1": _draft("d1", "first")})
    # A second run loaded the same empty state and also created d1.
    merged = conversation.append(stored, {"user": "b"}, {"d1": _draft("d1", "second")})
    assert [t["user"] for t in merged["turns"]] == ["a", "b"]
    assert {d["content"] for d in merged["drafts"].values()} == {"first", "second"}
    assert merged["next_draft"] == 3


def test_append_prunes_old_turns_and_drafts():
    state = conversation.empty("c")
    for n in range(1, 50):
        state = conversation.append(state, {"user": str(n)}, {f"d{n}": _draft(f"d{n}")})
    assert len(state["turns"]) == conversation.KEEP_TURNS
    assert list(state["drafts"])[0] == "d30" and list(state["drafts"])[-1] == "d49"
    view = conversation.for_turn(state)
    assert len(view["turns"]) == conversation.CONTEXT_TURNS
    assert view["next_draft"] == 50


def test_key_is_safe():
    assert conversation.key("a/../b c") == "pr_marketing_chat/conversations/a____b_c.json"


def test_normalize_reply_handles_gateway_shapes():
    msg = {
        "content": [{"type": "text", "text": "Let me check. "}],
        "tool_calls": [
            {"id": "x", "function": {"name": "read_doc", "arguments": '{"path": "a"}'}},
            {"function": {"name": "list_docs", "arguments": {}}},
            {"id": "z", "function": {"name": "jira_issue", "arguments": "{bad"}},
        ],
    }
    out = normalize_reply(msg)
    assert out["content"] == "Let me check."
    assert out["tool_calls"][0] == {"id": "x", "name": "read_doc", "args": {"path": "a"}}
    assert out["tool_calls"][1]["id"] == "call_1"
    assert "_invalid_arguments" in out["tool_calls"][2]["args"]
    assert normalize_reply({"content": None}) == {"content": "", "tool_calls": []}


def test_flatten_tool_turns_for_the_final_answer():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "list_docs"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"},
    ]
    flat = flatten_tool_turns(messages)
    assert [m["role"] for m in flat] == ["user", "assistant", "user"]
    assert "list_docs" in flat[1]["content"] and "tool_calls" not in flat[1]


def test_model_ids(monkeypatch):
    monkeypatch.setenv("CHAT_MODEL", "openai/gpt-5.1")
    assert model_for("chat") == ("openai", "gpt-5.1")
    monkeypatch.setenv("CHAT_MODEL", "claude-sonnet-5-5")
    assert model_for("chat") == ("anthropic", "claude-sonnet-5-5")
