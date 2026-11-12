"""Small text helpers shared by the clients: secret redaction and size caps."""

import re

from utils.github_client import redact as _github_redact

# Atlassian API tokens, on top of the patterns github_client already scrubs.
_EXTRA_SECRET_RES = [re.compile(r"ATATT[A-Za-z0-9_=-]{20,}")]


def redact(text: str) -> str:
    text = _github_redact(text or "")
    for rx in _EXTRA_SECRET_RES:
        text = rx.sub("[REDACTED]", text)
    return text


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"
