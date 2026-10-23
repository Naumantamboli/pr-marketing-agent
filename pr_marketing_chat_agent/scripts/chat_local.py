"""Chat with the agent locally, without Temporal: the same turn loop, activities called directly.

    .venv/bin/python scripts/chat_local.py                 # interactive
    .venv/bin/python scripts/chat_local.py "What's new?"   # one or more scripted turns

Needs a filled .env (gateway, GITHUB_TOKEN, Jira). Files and conversation memory go to
./output (LOCAL_OUTPUT=true is forced). Every turn shares one conversation id.
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["LOCAL_OUTPUT"] = "true"

from aetherion_sdk import toolExecutor  # noqa: E402

from agent import workflow  # noqa: E402
from tools import activities  # noqa: E402

ACTIVITIES = {
    name: getattr(activities, name)
    for name in dir(activities)
    if name.endswith("_tool") and callable(getattr(activities, name))
}


async def _execute(name: str, payload: dict, **_options) -> dict:
    print(f"  · {name}", flush=True)
    return await ACTIVITIES[name](payload)


def _show(result) -> None:
    if isinstance(result, str):
        print(f"\nAssistant:\n{result}\n")
        return
    for item in result["artifacts"]:
        if item.get("type") == "s3_download_link":
            print(f"  [file] {item['title']}: {ROOT / 'output' / item['file_key']}")
        else:
            print(f"\nAssistant:\n{item['text']}\n")


async def main(scripted: list[str]) -> None:
    toolExecutor.execute = _execute  # type: ignore[method-assign]
    workflow._now = lambda: datetime.now(UTC)
    workflow.workflow.logger = logging.getLogger("workflow")  # Temporal's needs a workflow loop
    conversation_id = f"local-{uuid.uuid4().hex[:8]}"
    print(f"Conversation {conversation_id}. Empty line or Ctrl-D to quit.\n")
    turns = iter(scripted) if scripted else None
    while True:
        if turns is not None:
            message = next(turns, "")
            if message:
                print(f"You: {message}")
        else:
            try:
                message = input("You: ").strip()
            except EOFError:
                message = ""
        if not message:
            return
        workflow._run_id = lambda: f"run-{uuid.uuid4().hex[:8]}"
        result = await workflow.run_turn(
            {"requirement": message, "task_metadata": {"conversation_id": conversation_id}}
        )
        _show(result)
        if os.getenv("CHAT_LOCAL_DEBUG"):
            print(json.dumps(result, indent=1, default=str)[:2000])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
