"""The agent as a real Temporal workflow (sandboxed, like the platform worker) with fake
activities. Catches what plain unit tests can't: sandbox-forbidden imports or calls,
non-serialisable payloads and determinism problems. Needs the `temporal` CLI on PATH."""

import shutil
import uuid
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agent import workflow as wf

TEMPORAL = shutil.which("temporal")
pytestmark = pytest.mark.skipif(not TEMPORAL, reason="temporal CLI not installed")

DECISIONS = [
    [{"id": "a", "name": "read_doc", "args": {"path": "content/docs/connectors.mdx"}}],
    [
        {
            "id": "b",
            "name": "write_draft",
            "args": {
                "format": "linkedin_post",
                "topic": "Jira",
                "source_refs": ["content/docs/connectors.mdx"],
            },
        },
        {
            "id": "c",
            "name": "make_social_visual",
            "args": {"draft_id": "d1", "layout": "headline_dark"},
        },
    ],
    [
        {
            "id": "d",
            "name": "make_social_visual",
            "args": {"draft_id": "d1", "layout": "headline_dark"},
        }
    ],
    None,  # final reply
]


def fake_activities(state: dict[str, Any]) -> list:
    @activity.defn(name="load_conversation_tool")
    async def load(payload: dict) -> dict:
        return {"turns": [], "drafts": {}, "next_draft": 1}

    @activity.defn(name="save_conversation_tool")
    async def save(payload: dict) -> dict:
        state["saved"] = payload
        return {}

    @activity.defn(name="chat_decide_tool")
    async def decide(payload: dict) -> dict:
        step = len(state.setdefault("decides", []))
        state["decides"].append(payload)
        calls = DECISIONS[step]
        if calls is None:
            return {"content": "Here is your post and image.", "tool_calls": []}
        return {"content": "", "tool_calls": calls}

    @activity.defn(name="docs_read_tool")
    async def read(payload: dict) -> dict:
        return {
            "path": payload["path"],
            "title": "Connectors",
            "content": "Connect Jira from Apps.",
        }

    @activity.defn(name="write_draft_tool")
    async def write(payload: dict) -> dict:
        return {
            "format": "linkedin_post",
            "title": "Jira",
            "content": "Connect Jira once.",
            "source_refs": payload["source_refs"],
        }

    @activity.defn(name="render_social_tool")
    async def render(payload: dict) -> dict:
        key = f"{payload['key_base']}-{payload['layout']}.png"
        return {
            "files": [
                {"type": "s3_download_link", "file_key": key, "title": "img", "extension": "png"}
            ]
        }

    @activity.defn(name="render_blog_tool")
    async def render_blog(payload: dict) -> dict:
        key = f"{payload['key_base']}-blog.pdf"
        return {
            "files": [
                {"type": "s3_download_link", "file_key": key, "title": "pdf", "extension": "pdf"}
            ]
        }

    return [load, save, decide, read, write, render, render_blog]


async def test_turn_runs_in_the_workflow_sandbox(monkeypatch):
    state: dict[str, Any] = {}
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL) as env:
        client: Client = env.client
        # The sandbox re-imports the SDK and reads the queue from the env, which a loaded
        # .env may have set; pin it so the fake activities' worker listens on the same one.
        tool_queue = "chat-agent-test-tools"
        monkeypatch.setenv("AETHERION_TOOL_TASK_QUEUE", tool_queue)
        async with (
            Worker(
                client,
                task_queue="agents",
                workflows=[wf.PrMarketingChatAgent._definition.temporal_class],
            ),
            Worker(client, task_queue=tool_queue, activities=fake_activities(state)),
        ):
            result = await client.execute_workflow(
                wf.NAME,
                {
                    "requirement": "Post about the Jira connector",
                    "task_metadata": {"conversation_id": "c-1", "customer_id": "cust"},
                },
                id=f"chat-{uuid.uuid4()}",
                task_queue="agents",
            )
    links = [a for a in result["artifacts"] if a.get("type") == "s3_download_link"]
    assert result["artifacts"][0] == {"type": "chat_reply", "text": "Here is your post and image."}
    # Step 2 rendered before d1 existed (same batch as write_draft): that call errors back to
    # the model, which retries in step 3.
    tool_results = [m for m in state["decides"][2]["messages"] if m["role"] == "tool"]
    assert "No draft 'd1'" in tool_results[-1]["content"]
    assert links[0]["file_key"].endswith("-3-d1-headline_dark.png")
    # Word/PDF was still owed for d1, so the turn made it after the reply.
    assert [link["file_key"].rsplit("-", 2)[-2:] for link in links[1:]] == [["d1", "blog.pdf"]]
    assert list(state["saved"]["drafts"]) == ["d1"]
    assert state["saved"]["turn"]["source_refs"] == ["content/docs/connectors.mdx"]
