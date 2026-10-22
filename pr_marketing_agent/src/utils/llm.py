"""Gateway-routed Claude calls that return validated pydantic objects."""

import asyncio
import json
import os
import re
from typing import TypeVar

from agent_lib.llm.llm_factory import LlmFactory
from common_lib.models.embeddings import Reference
from common_lib.utils.logger import setup_logger
from pydantic import BaseModel, ValidationError

logger = setup_logger(__name__)

AGENT_NAME = "PrMarketingAgent"

# Step -> (env var, default model).
_MODELS = {
    "triage": ("TRIAGE_MODEL", "claude-haiku-5-5"),
    "verify": ("VERIFY_MODEL", "claude-haiku-5-5"),
    "analysis": ("ANALYSIS_MODEL", "claude-sonnet-5-5"),
    "content": ("CONTENT_MODEL", "claude-sonnet-5-5"),
    "longform": ("LONGFORM_MODEL", "claude-opus-5-5"),
}

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def model_for(step: str) -> str:
    env, default = _MODELS[step]
    return os.getenv(env) or default


def _extract_json(text: str) -> str:
    text = _FENCE_RE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _call(model: str, prompt: str, label: str) -> str:
    # build_config picks the gateway provider from the model name (claude-* -> anthropic/…).
    client = LlmFactory.get_llm_connection(LlmFactory.build_config(model), agent_name=AGENT_NAME)
    response = client.call_llm_sync(Reference(agent=AGENT_NAME, filename=label), prompt)
    content = response.content if hasattr(response, "content") else str(response)
    if isinstance(content, list):
        content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return content


async def call_structured[T: BaseModel](step: str, prompt: str, schema: type[T], label: str) -> T:
    """Ask for JSON matching `schema`; one repair retry on invalid output."""
    model = model_for(step)
    instructions = (
        f"{prompt}\n\nRespond with ONLY a JSON object (no prose, no code fences) that "
        f"validates against this JSON Schema:\n{json.dumps(schema.model_json_schema())}"
    )
    raw = await asyncio.to_thread(_call, model, instructions, label)
    try:
        return schema.model_validate_json(_extract_json(raw))
    except ValidationError as err:
        logger.warning("%s: invalid JSON from %s, retrying once: %s", label, model, err)
        repair = (
            f"{instructions}\n\nYour previous answer was invalid:\n{raw[:4000]}\n\n"
            f"Errors:\n{err}\n\nReturn the corrected JSON object only."
        )
        raw = await asyncio.to_thread(_call, model, repair, label)
        return schema.model_validate_json(_extract_json(raw))
