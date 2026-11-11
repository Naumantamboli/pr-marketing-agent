import pytest

from utils import catalog as cat
from utils import overview

AT = "2026-11-11T10:00:00+00:00"


@pytest.fixture(autouse=True)
def store(monkeypatch):
    data = {}
    monkeypatch.setattr(overview.storage, "get", data.get)
    monkeypatch.setattr(
        overview.storage, "put", lambda key, body, _type: data.__setitem__(key, body)
    )
    return data


@pytest.fixture
def catalog():
    c = cat.empty("aetherion")
    for slug, name in [("chat-agents", "Chat agents"), ("connectors", "Connectors")]:
        c = cat.upsert_feature(c, slug, name=name, summary="x")
    return c


def _draft(**changes):
    base = {
        "identity": {"what_it_is": "A platform for building and running AI agents."},
        "capabilities": [
            {"name": "Chat agents", "enables": "Talk to agents", "feature": "chat-agents"}
        ],
        "architecture": [{"component": "SDK", "role": "Write agents in Python"}],
        "workflows": [
            {"name": "Build an agent", "steps": ["write", "publish"], "features": ["chat-agents"]}
        ],
        "relationships": [
            {"from": "chat-agents", "to": "connectors", "how": "Agents use connectors"}
        ],
        "constraints": [],
        "coverage": {"capabilities": "partial", "deployment": "missing"},
    }
    return {**base, **changes}


def test_nothing_published_at_first():
    assert overview.get_published() is None
    assert overview.head()["latest"] == 0


def test_draft_publish_and_rollback(catalog):
    v1 = overview.save_draft(_draft(), catalog, at=AT, reason="bootstrap")
    assert v1 == 1
    assert overview.get_published() is None  # a draft isn't visible until published
    assert overview.head()["pending"] == 1

    overview.publish(1, at=AT, approved_by="sme:ana")
    assert overview.get_published()["version"] == 1
    assert overview.head()["pending"] is None

    changed = _draft(constraints=["Max 30 PRs per run"])
    v2 = overview.save_draft(changed, catalog, at=AT, reason="pr:a/b/42")
    overview.publish(v2, at=AT, approved_by="sme:ana")
    assert overview.get_published()["constraints"] == ["Max 30 PRs per run"]

    overview.publish(1, at=AT, approved_by="sme:ana")  # roll back
    assert overview.get_published()["version"] == 1
    assert overview.get_version(2)["constraints"] == ["Max 30 PRs per run"]  # kept
    events = [h["event"] for h in overview.head()["history"]]
    assert events == ["draft", "publish", "draft", "publish", "publish"]


def test_discarded_draft_is_kept_but_not_pending(catalog):
    overview.save_draft(_draft(), catalog, at=AT, reason="x")
    overview.discard_pending(at=AT, by="sme:ana", reason="wrong")
    assert overview.head()["pending"] is None
    assert overview.get_version(1)
    assert overview.save_draft(_draft(), catalog, at=AT, reason="y") == 2  # numbers never reused


def test_invalid_drafts_are_refused(catalog):
    bad = _draft(
        identity={},
        capabilities=[{"name": "Canvas", "feature": "canvas"}],
        coverage={"architecture": "great"},
    )
    del bad["constraints"]
    with pytest.raises(ValueError) as err:
        overview.save_draft(bad, catalog, at=AT, reason="x")
    message = str(err.value)
    for part in ["'constraints'", "what_it_is", "'canvas'", "'great'"]:
        assert part in message
    assert overview.head()["latest"] == 0


def test_publishing_a_missing_version_fails():
    with pytest.raises(ValueError):
        overview.publish(5, at=AT, approved_by="x")


def test_sections_for_a_feature():
    assert overview.sections_for(_draft(), "chat-agents") == [
        "capabilities",
        "workflows",
        "relationships",
    ]
    assert overview.sections_for(_draft(), "connectors") == ["relationships"]
    assert overview.linked_features(_draft()) == {"chat-agents", "connectors"}
