"""Small text helpers shared by the clients: secret redaction and size caps."""

import re

# Docs and tickets can still paste credentials; scrub before text reaches the LLM.
_SECRET_RES = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ATATT[A-Za-z0-9_=-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)((?:password|passwd|secret|api[_-]?key|token|client[_-]?secret)\s*[:=]\s*)"
        r"['\"]?[^'\"\s]{6,}"
    ),
]


def redact(text: str) -> str:
    for rx in _SECRET_RES:
        text = rx.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"
