"""Feature knowledge in Memsy: one actor per feature, one validated fact per event.

Memsy builds memories from events itself and can't edit or delete them, so:
- each event carries {feature, fact_id, fact_v, ...} as metadata;
- search results are kept only when every source event is the catalog's current version
  (`catalog.is_current`), which hides changed and withdrawn facts;
- the answer quotes the original event text (include_source_events), not Memsy's rewrite.
"""

import json
import os
import re
from typing import Any

from common_lib.utils.logger import setup_logger

from utils import catalog as cat

logger = setup_logger(__name__)

MAX_CONTENT_CHARS = 8000  # Memsy event content limit
MAX_METADATA_CHARS = 4096
INGEST_BATCH = 100
SEARCH_MAX = 100  # Memsy's upper bound for `limit`
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_\-:.@/]")


def client():
    """An AsyncMemsyClient from MEMSY_API_KEY / MEMSY_BASE_URL; use with `async with`."""
    from memsy import AsyncMemsyClient

    key = os.getenv("MEMSY_API_KEY")
    if not key:
        raise ValueError("MEMSY_API_KEY is not set, so feature knowledge can't be reached.")
    return AsyncMemsyClient(
        base_url=os.getenv("MEMSY_BASE_URL", "https://api.memsy.io/v1"), api_key=key
    )


def safe_id(value: str) -> str:
    """A session id Memsy can search by: only [A-Za-z0-9_-:.@/], at most 256 chars."""
    return _SAFE_ID_RE.sub("_", value or "none")[: cat.MAX_ID_CHARS]


def fact_event(catalog: dict[str, Any], slug: str, fact: dict[str, Any], version: int):
    """The Memsy event for one validated fact.

    fact: {fact_id, section, text, source, [session], [pr], [ts]}. `session` groups the
    events of one change (e.g. "pr:owner/repo/42"); `ts` is when the change happened
    (merge or page update time), not when we send it.
    """
    from memsy import EventPayload

    entry = cat.feature(catalog, slug)
    cat.check_fact_id(fact["fact_id"], slug)
    text = (fact.get("text") or "").strip()
    if not text:
        raise ValueError(f"{fact['fact_id']}: fact text is empty.")
    # Name the feature in the text so each memory stands on its own.
    content = f"{entry['name']}: {text}"
    if len(content) > MAX_CONTENT_CHARS:
        raise ValueError(
            f"{fact['fact_id']}: {len(content)} chars; split it (max {MAX_CONTENT_CHARS})."
        )
    metadata = json.dumps(
        {
            "feature": slug,
            "fact_id": fact["fact_id"],
            "fact_v": version,
            "section": fact.get("section"),
            "source": fact.get("source"),
            "pr": fact.get("pr"),
        },
        ensure_ascii=False,
    )
    if len(metadata) > MAX_METADATA_CHARS:
        raise ValueError(f"{fact['fact_id']}: metadata over {MAX_METADATA_CHARS} chars.")
    return EventPayload(
        actor_id=entry["actor_id"],
        session_id=safe_id(fact.get("session") or "bootstrap"),
        kind="app_event",
        content=content,
        ts=fact.get("ts"),
        metadata=metadata,
    )


async def ingest_facts(
    memsy, catalog: dict[str, Any], slug: str, facts: list[tuple[dict[str, Any], int]]
) -> list[str]:
    """Send (fact, version) pairs for one feature; returns event ids to wait on.
    The catalog is NOT changed here: call `catalog.set_facts` once `pending()` is empty."""
    events = [fact_event(catalog, slug, fact, version) for fact, version in facts]
    ids: list[str] = []
    for start in range(0, len(events), INGEST_BATCH):
        ids += (await memsy.ingest(events[start : start + INGEST_BATCH])).event_ids
    return ids


async def processing(memsy, event_ids: list[str]) -> dict[str, list[str]]:
    """Which events Memsy has finished: {completed, pending, failed}."""
    if not event_ids:
        return {"completed": [], "pending": [], "failed": []}
    status = await memsy.status(event_ids)
    return {
        "completed": list(status.completed_ids),
        "pending": list(status.pending_ids),
        "failed": list(status.failed_ids),
    }


def keep_current(results: list[Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Search results whose every source event is a current fact, as plain dicts."""
    kept = []
    for r in results:
        sources = r.source_metadata
        metas = [s.get("metadata") for s in sources]
        # Memsy reports at most 5 sources; if a memory has more we can't check them all.
        if not metas or len(r.source_event_ids) > len(sources):
            continue
        if not all(cat.is_current(catalog, m) for m in metas):
            continue
        originals = {e.event_id: e.content for e in r.source_events}
        kept.append(
            {
                "memory_id": r.id,
                "text": r.content,
                "score": r.score,
                "feature": metas[0]["feature"],
                "facts": [
                    {k: m.get(k) for k in ("fact_id", "fact_v", "section", "source", "pr")}
                    for m in metas
                ],
                "sources": [
                    originals[s["event_id"]] for s in sources if s.get("event_id") in originals
                ],
            }
        )
    return kept


async def search_feature(
    memsy, catalog: dict[str, Any], slug: str, query: str, limit: int = 8
) -> list[dict[str, Any]]:
    """Search one feature's actor. Asks for extra results because old versions of the same
    facts compete for the top places and are then dropped."""
    entry = cat.feature(catalog, slug)
    response = await memsy.search(
        query,
        actor_id=entry["actor_id"],
        limit=min(SEARCH_MAX, limit * 3),
        include_source_events=True,
    )
    return keep_current(response.results, catalog)[:limit]


async def search_all(
    memsy, catalog: dict[str, Any], query: str, limit: int = 8
) -> list[dict[str, Any]]:
    """Org-wide search for questions that name no feature. Anything that isn't a current
    feature fact (old versions, user memories, other actors) is dropped."""
    response = await memsy.search(
        query, limit=min(SEARCH_MAX, limit * 5), include_source_events=True
    )
    return keep_current(response.results, catalog)[:limit]
