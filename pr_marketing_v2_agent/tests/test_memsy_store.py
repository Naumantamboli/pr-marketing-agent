import json

import pytest
from memsy import IngestResponse, SearchResponse, StatusResponse

from utils import catalog as cat
from utils import memsy_store

AT = "2026-11-11T10:00:00+00:00"
FID = "slack-connector.limits.max-rows"


@pytest.fixture
def catalog():
    c = cat.upsert_feature(
        cat.empty("aetherion"), "slack-connector", name="Slack connector", summary="x"
    )
    c = cat.upsert_feature(c, "canvas", name="Canvas", summary="y")
    c = cat.set_facts(c, "slack-connector", {FID: 2}, at=AT, source="x")
    return cat.set_facts(c, "canvas", {"canvas.overview.what": 1}, at=AT, source="x")


def _meta(feature, fact_id, v):
    return {
        "feature": feature,
        "fact_id": fact_id,
        "fact_v": v,
        "section": "limitations",
        "source": "confluence:1",
    }


def _result(memory_id, sources, score=0.9, extra_ids=0):
    """A search result as the Memsy API returns it, parsed by the real SDK models.
    sources: list of (event_id, metadata or None, original text)."""
    return {
        "id": memory_id,
        "content": f"memory {memory_id}",
        "score": score,
        "metadata": {
            "source_event_ids": [s[0] for s in sources] + [f"x{i}" for i in range(extra_ids)],
            "source_metadata": [
                {"event_id": eid, "metadata": meta}
                if meta is not None
                else {"event_id": eid, "raw": "n/a"}
                for eid, meta, _ in sources
            ],
            "source_events": [
                {"event_id": eid, "kind": "app_event", "content": text} for eid, _, text in sources
            ],
        },
    }


class FakeMemsy:
    def __init__(self, results=()):
        self.results = list(results)
        self.searches = []
        self.ingested = []

    async def search(self, query, **kwargs):
        self.searches.append({"query": query, **kwargs})
        return SearchResponse.from_dict({"results": self.results})

    async def ingest(self, events):
        self.ingested.append(events)
        start = sum(len(b) for b in self.ingested[:-1])
        return IngestResponse.from_dict(
            {"event_ids": [f"e{start + i}" for i in range(len(events))]}
        )

    async def status(self, event_ids):
        return StatusResponse.from_dict(
            {
                "completedIds": event_ids[:-1],
                "pendingIds": event_ids[-1:],
                "failedIds": [],
                "total": len(event_ids),
            }
        )


def test_fact_event_shape(catalog):
    event = memsy_store.fact_event(
        catalog,
        "slack-connector",
        {
            "fact_id": FID,
            "section": "limitations",
            "text": "Exports are limited to 1M rows on the Enterprise plan.",
            "source": "confluence:123#limitations",
            "session": "pr:CalfusResearch/airbyte_connectors#42",
            "pr": "CalfusResearch/airbyte_connectors/42",
            "ts": "2026-11-10T09:00:00Z",
        },
        3,
    )
    assert event.actor_id == "feature:aetherion:slack-connector"
    assert event.kind == "app_event"
    assert (
        event.content == "Slack connector: Exports are limited to 1M rows on the Enterprise plan."
    )
    assert event.session_id == "pr:CalfusResearch/airbyte_connectors_42"  # '#' isn't searchable
    assert event.ts == "2026-11-10T09:00:00Z"
    meta = json.loads(event.metadata)
    assert meta == {
        "feature": "slack-connector",
        "fact_id": FID,
        "fact_v": 3,
        "section": "limitations",
        "source": "confluence:123#limitations",
        "pr": "CalfusResearch/airbyte_connectors/42",
    }


@pytest.mark.parametrize(
    "fact",
    [
        {"fact_id": FID, "text": "  "},
        {"fact_id": FID, "text": "x" * 8000},
        {"fact_id": "canvas.overview.what", "text": "wrong feature"},
    ],
)
def test_bad_facts_are_refused(catalog, fact):
    with pytest.raises(ValueError):
        memsy_store.fact_event(catalog, "slack-connector", fact, 1)


async def test_ingest_batches_and_returns_event_ids(catalog, monkeypatch):
    monkeypatch.setattr(memsy_store, "INGEST_BATCH", 2)
    memsy = FakeMemsy()
    facts = [({"fact_id": f"slack-connector.s.f{i}", "text": f"fact {i}"}, 1) for i in range(5)]
    ids = await memsy_store.ingest_facts(memsy, catalog, "slack-connector", facts)
    assert ids == ["e0", "e1", "e2", "e3", "e4"]
    assert [len(b) for b in memsy.ingested] == [2, 2, 1]


async def test_processing_reports_pending(catalog):
    state = await memsy_store.processing(FakeMemsy(), ["e1", "e2"])
    assert state == {"completed": ["e1"], "pending": ["e2"], "failed": []}
    assert await memsy_store.processing(FakeMemsy(), []) == {
        "completed": [],
        "pending": [],
        "failed": [],
    }


def test_keep_current_filters_old_unknown_and_merged_memories(catalog):
    results = SearchResponse.from_dict(
        {
            "results": [
                _result("current", [("e1", _meta("slack-connector", FID, 2), "1M rows")]),
                _result("old", [("e0", _meta("slack-connector", FID, 1), "100k rows")]),
                # one memory built from the old and the new version: the old one could leak
                _result(
                    "merged",
                    [
                        ("e0", _meta("slack-connector", FID, 1), "a"),
                        ("e1", _meta("slack-connector", FID, 2), "b"),
                    ],
                ),
                _result("user-memory", [("u1", None, "I prefer bullets")]),
                _result("no-sources", []),
                # more sources than Memsy reports (it caps at 5): can't check them all
                _result("too-many", [("e1", _meta("slack-connector", FID, 2), "b")], extra_ids=5),
                _result(
                    "canvas", [("c1", _meta("canvas", "canvas.overview.what", 1), "Canvas is…")]
                ),
            ]
        }
    ).results
    kept = memsy_store.keep_current(results, catalog)
    assert [k["memory_id"] for k in kept] == ["current", "canvas"]
    assert kept[0]["sources"] == ["1M rows"]
    assert kept[0]["facts"][0] == {
        "fact_id": FID,
        "fact_v": 2,
        "section": "limitations",
        "source": "confluence:1",
        "pr": None,
    }


async def test_search_feature_uses_its_actor_and_over_fetches(catalog):
    memsy = FakeMemsy([_result("m1", [("e1", _meta("slack-connector", FID, 2), "1M rows")])])
    found = await memsy_store.search_feature(
        memsy, catalog, "slack-connector", "row limit", limit=4
    )
    assert [f["memory_id"] for f in found] == ["m1"]
    call = memsy.searches[0]
    assert call["actor_id"] == "feature:aetherion:slack-connector"
    assert call["limit"] == 12 and call["include_source_events"] is True


async def test_search_all_has_no_actor_and_caps_limit(catalog):
    memsy = FakeMemsy()
    await memsy_store.search_all(memsy, catalog, "429 errors", limit=30)
    assert "actor_id" not in memsy.searches[0]
    assert memsy.searches[0]["limit"] == 100


async def test_search_unknown_feature_fails(catalog):
    with pytest.raises(ValueError):
        await memsy_store.search_feature(FakeMemsy(), catalog, "nope", "q")


def test_client_needs_an_api_key(monkeypatch):
    monkeypatch.delenv("MEMSY_API_KEY", raising=False)
    with pytest.raises(ValueError):
        memsy_store.client()
