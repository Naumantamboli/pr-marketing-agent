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
    gh = _FakeGitHub(
        {
            "a/web": [
                {
                    "number": 1,
                    "merged_at": "2026-10-20T00:00:00Z",
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
