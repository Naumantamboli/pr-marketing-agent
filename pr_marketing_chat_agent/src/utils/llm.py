"""Gateway LLM calls: one tool-calling chat step, and structured JSON answers.

Models are "provider/model" ids from the env (bare names are treated as Anthropic).
"""

import json
import os
import re
from typing import Any

from agent_lib.gateway.ai import AiGatewayClient
from common_lib.utils.logger import setup_logger
from pydantic import BaseModel, ValidationError

logger = setup_logger(__name__)

# Step -> (env var, default model).
_MODELS = {
    "chat": ("CHAT_MODEL", "anthropic/claude-sonnet-5-5"),
    "content": ("CONTENT_MODEL", "anthropic/claude-sonnet-5-5"),
    "longform": ("LONGFORM_MODEL", "anthropic/claude-opus-5-5"),
    "verify": ("VERIFY_MODEL", "anthropic/claude-haiku-5-5"),
}

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def model_for(step: str) -> tuple[str, str]:
    env, default = _MODELS[step]
    model = os.getenv(env) or default
    provider, _, name = model.partition("/")
    return (provider, name) if name else ("anthropic", provider)


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return content or ""


def normalize_reply(msg: dict[str, Any]) -> dict[str, Any]:
    """Gateway reply -> {"content": str, "tool_calls": [{"id", "name", "args": dict}]}."""
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw = fn.get("arguments")
        if isinstance(raw, dict):
            args = raw
        else:
            try:
                args = json.loads(raw or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_arguments": str(raw)[:500]}
        calls.append({"id": tc.get("id") or f"call_{i}", "name": fn.get("name", ""), "args": args})
    return {"content": _text(msg.get("content")).strip(), "tool_calls": calls}


def flatten_tool_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite tool-call history as plain text, for a final answer with tools switched off."""
    flat: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            names = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
            flat.append(
                {"role": "assistant", "content": (m.get("content") or "") + f"\n[used: {names}]"}
            )
        elif m["role"] == "tool":
            flat.append({"role": "user", "content": f"[tool result]\n{m['content']}"})
        else:
            flat.append(m)
    return flat


async def decide(
    system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """One chat step. With tools=None the model must answer in text."""
    provider, model = model_for("chat")
    convo = [{"role": "system", "content": system}]
    async with AiGatewayClient() as client:
        if tools:
            msg = await client.chat_with_tools(provider, model, convo + messages, tools)
        else:
            msg = await client.chat_with_messages(
                provider, model, convo + flatten_tool_turns(messages)
            )
    return normalize_reply(msg)


def _extract_json(text: str) -> str:
    text = _FENCE_RE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


async def structured[T: BaseModel](step: str, prompt: str, schema: type[T]) -> T:
    """Ask for JSON matching `schema`; one repair retry on invalid output."""
    provider, model = model_for(step)
    instructions = (
        f"{prompt}\n\nRespond with ONLY a JSON object (no prose, no code fences) that "
        f"validates against this JSON Schema:\n{json.dumps(schema.model_json_schema())}"
    )
    async with AiGatewayClient() as client:
        raw = _text((await client.chat(provider, model, instructions))["content"])
        try:
            return schema.model_validate_json(_extract_json(raw))
        except ValidationError as err:
            logger.warning("%s: invalid JSON from %s, retrying once: %s", step, model, err)
            repair = (
                f"{instructions}\n\nYour previous answer was invalid:\n{raw[:4000]}\n\n"
                f"Errors:\n{err}\n\nReturn the corrected JSON object only."
            )
            raw = _text((await client.chat(provider, model, repair))["content"])
            return schema.model_validate_json(_extract_json(raw))
