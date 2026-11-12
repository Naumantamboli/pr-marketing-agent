"""Structured LLM calls through the AI Gateway.

Each step has its own model, set by env var ("provider/model"; a bare name means Anthropic).
The verify and check steps default to a different model from the ones that write, so the
reviewer doesn't share the writer's blind spots.
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
MODELS = {
    "route": ("ROUTE_MODEL", "anthropic/claude-haiku-5-5"),
    "extract": ("EXTRACT_MODEL", "anthropic/claude-sonnet-5-5"),
    "verify": ("VERIFY_MODEL", "anthropic/claude-opus-5-5"),
    "compose": ("COMPOSE_MODEL", "anthropic/claude-sonnet-5-5"),
    "check": ("CHECK_MODEL", "anthropic/claude-opus-5-5"),
}

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def model_for(step: str) -> tuple[str, str]:
    env, default = MODELS[step]
    model = os.getenv(env) or default
    provider, _, name = model.partition("/")
    return (provider, name) if name else ("anthropic", provider)


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return content or ""


def extract_json(text: str) -> str:
    text = _FENCE_RE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


async def structured[T: BaseModel](step: str, system: str, prompt: str, schema: type[T]) -> T:
    """Ask for JSON matching `schema`, with one repair retry on invalid output."""
    provider, model = model_for(step)
    instructions = (
        f"{prompt}\n\nRespond with ONLY a JSON object (no prose, no code fences) that "
        f"validates against this JSON Schema:\n{json.dumps(schema.model_json_schema())}"
    )
    async with AiGatewayClient() as client:
        reply = await client.chat(
            provider=provider,
            model_name=model,
            prompt=instructions,
            system_prompt=system,
            temperature=0,
        )
        raw = _text(reply["content"])
        try:
            return schema.model_validate_json(extract_json(raw))
        except ValidationError as err:
            logger.warning("%s: invalid JSON from %s, retrying once: %s", step, model, err)
            repair = (
                f"{instructions}\n\nYour previous answer was invalid:\n{raw[:4000]}\n\n"
                f"Errors:\n{err}\n\nReturn the corrected JSON object only."
            )
            reply = await client.chat(
                provider=provider,
                model_name=model,
                prompt=repair,
                system_prompt=system,
                temperature=0,
            )
            return schema.model_validate_json(extract_json(_text(reply["content"])))
