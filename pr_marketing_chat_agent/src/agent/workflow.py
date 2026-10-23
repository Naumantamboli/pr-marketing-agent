"""PrMarketingChatAgent: a chat agent that explains what the product team is building and turns
it into on-brand posts, blogs and visuals.

The chat runs one workflow per user message (chat_direct): the message arrives as
`requirement` and earlier turns are loaded from the agent's own conversation store. Each turn
loops: the model either replies or asks for tools; tools run as activities and their results go
back to the model. Workflow code stays deterministic: no I/O, no env, no third-party imports.
"""

import asyncio
import json
from datetime import timedelta
from typing import Any

from aetherion_sdk import agent, register, toolExecutor
from aetherion_sdk.decorators import REGISTERED_TOOLS
from temporalio import workflow
from temporalio.common import RetryPolicy

NAME = "PrMarketingChatAgent"

# Bad input won't fix itself on retry; the error goes back to the model instead.
_RETRY = RetryPolicy(maximum_attempts=3, non_retryable_error_types=["ValueError"])
_FAST = {"start_to_close_timeout": timedelta(minutes=2), "retry_policy": _RETRY}
_LLM = {"start_to_close_timeout": timedelta(minutes=4), "retry_policy": _RETRY}
_LONG = {"start_to_close_timeout": timedelta(minutes=10), "retry_policy": _RETRY}

MAX_STEPS = 8
# milkyway stops waiting for a chat run after 15 minutes and drops its result.
TURN_BUDGET = timedelta(minutes=8)
MAX_TOOL_RESULT_CHARS = 12000
HISTORY_REPLY_CHARS = 4000

# Model-facing tool name -> (activity, timeouts). Nothing outside this map can run.
TOOL_ACTIVITIES: dict[str, tuple[str, dict[str, Any]]] = {
    "list_docs": ("docs_index_tool", _FAST),
    "read_doc": ("docs_read_tool", _FAST),
    "whats_new_in_docs": ("docs_changes_tool", _FAST),
    "find_jira_work": ("jira_find_tool", _FAST),
    "jira_issue": ("jira_issue_tool", _FAST),
    "write_draft": ("write_draft_tool", _LONG),
    "make_social_visual": ("render_social_tool", _LLM),
    "make_blog_document": ("render_blog_tool", _FAST),
}

ERROR_REPLY = (
    "Sorry, something went wrong on my side and I couldn't finish that. "
    "Please try again, or rephrase the request."
)


def _now() -> Any:
    return workflow.now()


def _run_id() -> str:
    return workflow.info().workflow_id


def _clip(value: Any, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def _error_text(exc: BaseException) -> str:
    """The useful message from an activity failure (ActivityError wraps ApplicationError)."""
    cause = exc
    while getattr(cause, "cause", None) is not None:
        cause = cause.cause
    return getattr(cause, "message", None) or str(cause) or type(cause).__name__


def history_messages(
    turns: list[dict[str, Any]], requirement: str, uploads: list[str]
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for t in turns:
        messages.append({"role": "user", "content": t["user"]})
        messages.append({"role": "assistant", "content": _clip(t["reply"], HISTORY_REPLY_CHARS)})
    note = (
        f"\n\n[The user attached {len(uploads)} file(s); attachments can't be read yet.]"
        if uploads
        else ""
    )
    messages.append({"role": "user", "content": requirement + note})
    return messages


def assistant_message(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": decision["content"],
        "tool_calls": [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c["args"])},
            }
            for c in decision["tool_calls"]
        ],
    }


def result_shape(reply: str, files: list[dict[str, Any]]) -> Any:
    """Chat renders a bare string as markdown; with files only `artifacts` is rendered, so the
    reply rides in that list as a titled text item."""
    if not files:
        return reply
    return {"artifacts": [*files, {"title": "Details", "text": reply}]}


class Turn:
    """Everything one chat turn produces, kept in workflow memory until it is saved."""

    def __init__(self, ctx: dict[str, Any], loaded: dict[str, Any]):
        self.ctx = ctx
        self.drafts: dict[str, Any] = dict(loaded.get("drafts") or {})
        self.next_draft = int(loaded.get("next_draft") or len(self.drafts) + 1)
        self.new_drafts: dict[str, Any] = {}
        self.files: list[dict[str, Any]] = []
        self.source_refs: list[str] = []
        self.step = 0

    def catalog(self) -> dict[str, Any]:
        """Drafts trimmed for the model's draft list; full text stays in the workflow."""
        return {
            i: {
                "id": i,
                "format": d["format"],
                "title": d.get("title"),
                "content": d["content"][:200],
            }
            for i, d in self.drafts.items()
        }

    def _draft(self, draft_id: Any) -> dict[str, Any]:
        draft = self.drafts.get(str(draft_id or "").strip().lower())
        if not draft:
            known = ", ".join(self.drafts) or "none yet"
            raise ValueError(f"No draft {draft_id!r} in this conversation (drafts: {known}).")
        return draft

    def _payload(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Model arguments -> activity payload; drafts and storage targets come from the turn."""
        if "_invalid_arguments" in args:
            raise ValueError("The tool arguments were not valid JSON; call the tool again.")
        payload = dict(args)
        if name == "write_draft" and args.get("base_draft_id"):
            payload["base_draft"] = self._draft(args["base_draft_id"])
        if name in ("make_social_visual", "make_blog_document"):
            payload["draft"] = self._draft(args.get("draft_id"))
            payload["customer_id"] = self.ctx["customer_id"]
            # Same key on a retry (overwrites), a new one for every other call.
            payload["key_base"] = (
                f"pr_marketing_chat/{self.ctx['safe_conversation_id']}/"
                f"{self.ctx['run_id']}-{self.step}-{payload['draft']['id']}"
            )
        return payload

    def _absorb(self, name: str, args: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
        """Keep drafts, files and sources; return what the model should see."""
        if name == "read_doc":
            self.source_refs.append(out.get("path", ""))
        elif name == "jira_issue":
            self.source_refs.append(out.get("key", ""))
        elif name == "write_draft":
            draft_id = f"d{self.next_draft}"
            self.next_draft += 1
            draft = {**out, "id": draft_id, "created_at": _now().isoformat()}
            self.drafts[draft_id] = self.new_drafts[draft_id] = draft
            self.source_refs.extend(out.get("source_refs", []))
            return {"draft_id": draft_id, **{k: v for k, v in out.items() if k != "theme"}}
        elif name in ("make_social_visual", "make_blog_document"):
            self.files.extend(out.get("files", []))
            return {**out, "files": [f["title"] for f in out.get("files", [])]}
        return out

    async def call(self, name: str, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name not in TOOL_ACTIVITIES:
                raise ValueError(f"Unknown tool {name!r}.")
            activity, options = TOOL_ACTIVITIES[name]
            out = await toolExecutor.execute(activity, self._payload(name, args), **options)
            content = _clip(self._absorb(name, args, out))
        except Exception as exc:  # noqa: BLE001 - any failure goes back to the model
            content = _clip({"error": _error_text(exc)})
        return {"role": "tool", "tool_call_id": call_id, "content": content}


async def _decide(messages: list[dict[str, Any]], turn: Turn, allow_tools: bool) -> dict:
    return await toolExecutor.execute(
        "chat_decide_tool",
        {"messages": messages, "drafts": turn.catalog(), "allow_tools": allow_tools},
        **_LLM,
    )


async def converse(messages: list[dict[str, Any]], turn: Turn) -> str:
    """Model/tool loop for one turn; ends with a text reply."""
    started = _now()
    for _ in range(MAX_STEPS):
        if _now() - started > TURN_BUDGET:
            break
        decision = await _decide(messages, turn, allow_tools=True)
        if not decision["tool_calls"]:
            return decision["content"]
        turn.step += 1
        messages.append(assistant_message(decision))
        results = await asyncio.gather(
            *(turn.call(c["name"], c["id"], c["args"]) for c in decision["tool_calls"])
        )
        messages.extend(results)
    # Out of steps or time: answer with what has been gathered so far.
    final = await _decide(messages, turn, allow_tools=False)
    return final["content"]


async def run_turn(payload: dict[str, Any]) -> Any:
    requirement = str(payload.get("requirement") or payload.get("message") or "").strip()
    meta = payload.get("task_metadata") or {}
    run_id = _run_id()
    conversation_id = str(meta.get("conversation_id") or run_id)
    ctx = {
        "conversation_id": conversation_id,
        "safe_conversation_id": "".join(
            c if c.isalnum() or c in "-_" else "_" for c in conversation_id
        ),
        "customer_id": meta.get("customer_id"),
        "run_id": run_id,
    }
    uploads = [str(u) for u in payload.get("uploaded_files") or []]
    if not requirement:
        return "Hi! Ask me what the product team has shipped lately, or what a feature does."

    store = {"conversation_id": conversation_id, "customer_id": ctx["customer_id"]}
    loaded: dict[str, Any] = {}
    reply = ERROR_REPLY
    try:
        loaded = await toolExecutor.execute("load_conversation_tool", store, **_FAST)
    except Exception as exc:  # noqa: BLE001 - a lost history shouldn't block the answer
        workflow.logger.warning("Couldn't load conversation %s: %s", conversation_id, exc)
    turn = Turn(ctx, loaded)
    try:
        messages = history_messages(loaded.get("turns") or [], requirement, uploads)
        reply = await converse(messages, turn) or ERROR_REPLY
    except Exception as exc:  # noqa: BLE001 - always answer the user
        workflow.logger.error("Turn failed in %s: %s", conversation_id, exc)

    record = {
        "at": _now().isoformat(),
        "run_id": run_id,
        "user": requirement,
        "reply": reply,
        "source_refs": list(dict.fromkeys(r for r in turn.source_refs if r)),
        "files": [{"title": f["title"], "file_key": f["file_key"]} for f in turn.files],
        "uploads": uploads,
    }
    try:
        await toolExecutor.execute(
            "save_conversation_tool", {**store, "turn": record, "drafts": turn.new_drafts}, **_FAST
        )
    except Exception as exc:  # noqa: BLE001 - the reply still goes out
        workflow.logger.error("Couldn't save conversation %s: %s", conversation_id, exc)
    return result_shape(reply, turn.files)


@agent(name=NAME)
@register(
    name=NAME,
    description=(
        "Marketing assistant: explains in plain English what the product team has shipped or "
        "is building (from the product docs and Jira), and writes on-brand LinkedIn/X posts, "
        "blogs, release notes and emails, plus Calfus-branded social images (.png/.pptx) and "
        "blog documents (.docx/.pdf). Drafts are fact-checked and need human review."
    ),
    input_schema=[
        {"name": "requirement", "type": "text", "description": "The user's chat message."}
    ],
    output_schema=[
        {"name": "reply", "type": "text", "description": "Markdown reply, plus any files."}
    ],
    type="agent",
    chat_enabled=True,
    chat_direct=True,
)
async def PrMarketingChatAgent(payload: dict[str, Any]) -> Any:
    """One chat turn: {"requirement": message, "task_metadata": {conversation_id, ...}}."""
    return await run_turn(payload)


# @agent returns an AgentHandle without the @register metadata; the deployed worker's registry
# scan reads `_aetherion_register_metadata` off this module attribute, so re-attach it.
PrMarketingChatAgent._aetherion_register_metadata = REGISTERED_TOOLS[NAME]
