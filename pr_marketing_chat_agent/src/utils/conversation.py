"""Per-conversation memory: turns, drafts and files, stored as one JSON object per conversation.

Saving re-reads the stored object and appends, so two overlapping runs in one conversation
both keep their turn instead of the later one overwriting the earlier.
"""

from typing import Any

KEEP_TURNS = 40  # stored
KEEP_DRAFTS = 20
CONTEXT_TURNS = 12  # sent to the model
PREVIEW_CHARS = 120


def key(conversation_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in conversation_id)
    return f"pr_marketing_chat/conversations/{safe}.json"


def empty(conversation_id: str) -> dict[str, Any]:
    return {"conversation_id": conversation_id, "turns": [], "drafts": {}, "next_draft": 1}


def for_turn(state: dict[str, Any]) -> dict[str, Any]:
    """The slice of state a new turn needs: recent turns, drafts and the next draft number."""
    return {
        "turns": state["turns"][-CONTEXT_TURNS:],
        "drafts": state["drafts"],
        "next_draft": state.get("next_draft", len(state["drafts"]) + 1),
    }


def draft_catalog(drafts: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": d["id"],
            "format": d["format"],
            "title": d.get("title"),
            "preview": " ".join(d["content"].split())[:PREVIEW_CHARS],
        }
        for d in drafts.values()
    ]


def _draft_no(draft_id: str) -> int:
    digits = draft_id.lstrip("d")
    return int(digits) if digits.isdigit() else 0


def append(stored: dict[str, Any], turn: dict[str, Any], drafts: dict[str, Any]) -> dict[str, Any]:
    """Merge one finished turn into the stored state (stored may be newer than what we loaded)."""
    state = {**stored, "turns": [*stored["turns"], turn][-KEEP_TURNS:]}
    merged = dict(stored["drafts"])
    for draft_id, draft in drafts.items():
        if draft_id in merged and merged[draft_id]["content"] != draft["content"]:
            # An overlapping run used the same id; keep both.
            draft_id = f"d{max(_draft_no(i) for i in merged) + 1}"
            draft = {**draft, "id": draft_id}
        merged[draft_id] = draft
    kept = sorted(merged, key=_draft_no)[-KEEP_DRAFTS:]
    state["drafts"] = {i: merged[i] for i in kept}
    state["next_draft"] = max([stored.get("next_draft", 1)] + [_draft_no(i) + 1 for i in merged])
    return state
