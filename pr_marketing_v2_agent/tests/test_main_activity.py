from datetime import UTC, datetime, timedelta

import pytest

from tools import main_activity


class _FakeGitHub:
    def __init__(self, by_repo):
        self.by_repo = by_repo
        self.since = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def list_merged_prs(self, repo, since, limit):
        self.since[repo] = since
        return self.by_repo.get(repo, [])


def _fn(tool):
    """The undecorated function, whether or not the real SDK wraps it."""
    return getattr(tool, "fn", tool)


@pytest.fixture
def saved(monkeypatch):
    store = {}
    monkeypatch.setattr(main_activity.storage, "read_marker", store.get)
    monkeypatch.setattr(main_activity.storage, "write_marker", store.__setitem__)
    return store


async def test_first_run_uses_lookback_and_returns_markers(monkeypatch, saved):
    # Inside the 7-day lookback whatever day the test runs.
    merged_at = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    gh = _FakeGitHub(
        {
            "a/web": [
                {
                    "number": 1,
                    "merged_at": merged_at,
                    "type_check": {"decision": "include"},
                }
            ]
        }
    )
    monkeypatch.setattr(main_activity, "GitHubClient", lambda: gh)

    result = await _fn(main_activity.list_merged_prs_tool)(["a/web", "a/api"], 7)

    lookback = datetime.now(UTC) - timedelta(days=7)
    assert abs(gh.since["a/web"] - lookback) < timedelta(minutes=1)
    assert [p["number"] for p in result["prs"]] == [1]
    assert result["markers"]["a/web"]["numbers"] == [1]
    # No PRs in a/api: its first-run marker is still returned, anchoring the window.
    assert result["markers"]["a/api"]["numbers"] == []
    assert saved == {}  # listing never saves markers


async def test_later_run_reads_saved_marker(monkeypatch, saved):
    saved["a/web"] = {"merged_at": "2026-10-20T00:00:00Z", "numbers": [1]}
    gh = _FakeGitHub({})
    monkeypatch.setattr(main_activity, "GitHubClient", lambda: gh)

    await _fn(main_activity.list_merged_prs_tool)(["a/web"], 7)

    assert gh.since["a/web"] == datetime(2026, 10, 20, tzinfo=UTC)


async def test_save_markers_writes_each_repo(saved):
    await _fn(main_activity.save_markers_tool)(
        {"a/web": {"merged_at": "2026-10-20T00:00:00Z", "numbers": [1]}}
    )
    assert saved["a/web"]["numbers"] == [1]
    assert "saved_at" in saved["a/web"]


# ── Knowledge read tools ──


@pytest.fixture
def knowledge(monkeypatch):
    from utils import catalog as cat

    data = {}
    monkeypatch.setattr(main_activity.storage, "get", data.get)
    monkeypatch.setattr(
        main_activity.storage, "put", lambda key, body, _type: data.__setitem__(key, body)
    )
    c = cat.upsert_feature(
        cat.empty("aetherion"), "slack-connector", name="Slack connector", summary="x"
    )
    cat.save(c, at="2026-11-11T00:00:00Z")
    return data


async def test_overview_tool_before_and_after_publish(knowledge):
    from utils import catalog as cat
    from utils import overview

    assert await _fn(main_activity.get_overview_tool)({}) == {"available": False}
    draft = {
        "identity": {"what_it_is": "A platform."},
        "capabilities": [],
        "architecture": [],
        "workflows": [],
        "relationships": [],
        "constraints": [],
        "coverage": {},
    }
    version = overview.save_draft(draft, cat.load(), at="t", reason="x")
    overview.publish(version, at="t", approved_by="sme")
    result = await _fn(main_activity.get_overview_tool)({})
    assert result["available"] and result["overview"]["identity"]["what_it_is"] == "A platform."


async def test_feature_lookup_tools(knowledge):
    listed = await _fn(main_activity.list_features_tool)({})
    assert [f["slug"] for f in listed["features"]] == ["slack-connector"]
    found = await _fn(main_activity.find_feature_tool)(
        {"text": "how does the Slack connector work"}
    )
    assert found["matches"][0]["actor_id"] == "feature:aetherion:slack-connector"


async def test_search_tool_picks_feature_or_org_wide(knowledge, monkeypatch):
    calls = []

    class _Client:
        async def __aenter__(self):
            return "memsy"

        async def __aexit__(self, *exc):
            return None

    async def by_feature(memsy, catalog, slug, query, limit):
        calls.append(("feature", slug, query, limit))
        return []

    async def everywhere(memsy, catalog, query, limit):
        calls.append(("all", None, query, limit))
        return []

    monkeypatch.setattr(main_activity.memsy_store, "client", _Client)
    monkeypatch.setattr(main_activity.memsy_store, "search_feature", by_feature)
    monkeypatch.setattr(main_activity.memsy_store, "search_all", everywhere)
    search = _fn(main_activity.search_feature_knowledge_tool)
    await search({"query": "row limit", "feature": "slack-connector"})
    await search({"query": "429 errors", "limit": 99})
    assert calls == [
        ("feature", "slack-connector", "row limit", 8),
        ("all", None, "429 errors", 20),
    ]
    with pytest.raises(ValueError):
        await search({"query": "  "})
