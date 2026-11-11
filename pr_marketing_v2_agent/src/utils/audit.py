"""Audit trail in SDK storage: one never-edited JSON file per event (plan §11.2).

Keys sort by time: product_knowledge/audit/2026-11-11/20261111T101500Z-<id>.json
"""

import json
import uuid
from datetime import datetime
from typing import Any

from utils import storage
from utils.catalog import KNOWLEDGE_PREFIX

EVENTS = (
    "pr_detected",
    "sources_resolved",
    "bundle_built",
    "findings_created",
    "validation_decision",
    "sme_decision",
    "facts_ingested",
    "catalog_updated",
    "overview_drafted",
    "overview_published",
    "run_finished",
)


def record(event: str, data: dict[str, Any], *, at: datetime, event_id: str | None = None) -> str:
    """Write one audit event; returns its key. Pass a stable `event_id` (e.g. derived from
    the PR and step) so a retried step overwrites its own record instead of adding one."""
    if event not in EVENTS:
        raise ValueError(f"Unknown audit event {event!r}.")
    event_id = event_id or uuid.uuid4().hex
    stamp = at.strftime("%Y%m%dT%H%M%SZ")
    key = f"{KNOWLEDGE_PREFIX}/audit/{at:%Y-%m-%d}/{stamp}-{event_id}.json"
    body = {"event": event, "at": at.isoformat(), "event_id": event_id, **data}
    storage.put(key, json.dumps(body, ensure_ascii=False, default=str).encode(), "application/json")
    return key
