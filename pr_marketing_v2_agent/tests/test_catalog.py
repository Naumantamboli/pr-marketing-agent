import pytest

from utils import catalog as cat

AT = "2026-11-11T10:00:00+00:00"


@pytest.fixture
def store(monkeypatch):
    data = {}
    monkeypatch.setattr(cat.storage, "get", data.get)
    monkeypatch.setattr(cat.storage, "put", lambda key, body, _type: data.__setitem__(key, body))
    return data


def _with_slack():
    return cat.upsert_feature(
        cat.empty("aetherion"),
        "slack-connector",
        name="Slack connector",
        summary="Read and post Slack messages.",
        aliases=["Slack integration"],
    )


def test_actor_id_convention():
    assert cat.actor_id("slack-connector", "aetherion") == "feature:aetherion:slack-connector"


@pytest.mark.parametrize("slug", ["Slack", "slack_connector", "slack--x", "-slack", "", "a:b"])
def test_bad_slugs_are_refused(slug):
    with pytest.raises(ValueError):
        cat.actor_id(slug, "aetherion")


def test_slugify():
    assert cat.slugify("Slack Connector (v2)") == "slack-connector-v2"
    with pytest.raises(ValueError):
        cat.slugify("!!!")


def test_new_feature_entry():
    entry = _with_slack()["features"]["slack-connector"]
    assert entry["actor_id"] == "feature:aetherion:slack-connector"
    assert entry["state"] == "published"
    assert entry["facts"] == {} and entry["changes"] == []


def test_rename_keeps_slug_actor_and_old_name_as_alias():
    renamed = cat.upsert_feature(
        _with_slack(), "slack-connector", name="Slack app", summary="Same feature."
    )
    entry = renamed["features"]["slack-connector"]
    assert entry["actor_id"] == "feature:aetherion:slack-connector"
    assert entry["name"] == "Slack app"
    assert entry["aliases"] == ["Slack connector", "Slack integration"]


def test_upsert_does_not_change_its_input():
    original = _with_slack()
    cat.upsert_feature(original, "canvas", name="Canvas", summary="x")
    assert "canvas" not in original["features"]


def test_fact_versions_and_change_history():
    c = _with_slack()
    fid = "slack-connector.limits.max-rows"
    assert cat.next_version(c, "slack-connector", fid) == 1
    c = cat.set_facts(c, "slack-connector", {fid: 1}, at=AT, source="bootstrap")
    assert cat.next_version(c, "slack-connector", fid) == 2
    c = cat.set_facts(c, "slack-connector", {fid: 2}, at=AT, source="pr:a/b/42")
    entry = c["features"]["slack-connector"]
    assert entry["facts"] == {fid: 2}
    assert [(ch["from_v"], ch["to_v"], ch["source"]) for ch in entry["changes"]] == [
        (None, 1, "bootstrap"),
        (1, 2, "pr:a/b/42"),
    ]


def test_reapplying_the_same_version_changes_nothing():
    fid = "slack-connector.limits.max-rows"
    c = cat.set_facts(_with_slack(), "slack-connector", {fid: 1}, at=AT, source="pr:a/b/1")
    again = cat.set_facts(c, "slack-connector", {fid: 1}, at=AT, source="pr:a/b/1")
    assert again == c


def test_versions_never_go_backwards():
    fid = "slack-connector.limits.max-rows"
    c = cat.set_facts(_with_slack(), "slack-connector", {fid: 2}, at=AT, source="x")
    with pytest.raises(ValueError):
        cat.set_facts(c, "slack-connector", {fid: 1}, at=AT, source="x")


def test_fact_id_must_belong_to_the_feature():
    with pytest.raises(ValueError):
        cat.set_facts(_with_slack(), "slack-connector", {"canvas.limits.x": 1}, at=AT, source="x")


def test_withdrawn_fact_is_no_longer_current_and_restarts_above_old_version():
    fid = "slack-connector.limits.max-rows"
    c = cat.set_facts(_with_slack(), "slack-connector", {fid: 3}, at=AT, source="x")
    meta = {"feature": "slack-connector", "fact_id": fid, "fact_v": 3}
    assert cat.is_current(c, meta)
    c = cat.withdraw_facts(c, "slack-connector", [fid], at=AT, source="pr:a/b/9")
    assert not cat.is_current(c, meta)
    assert cat.next_version(c, "slack-connector", fid) == 4


@pytest.mark.parametrize(
    "meta",
    [
        None,
        "not a dict",
        {"feature": "canvas", "fact_id": "canvas.x.y", "fact_v": 1},  # unknown feature
        {"feature": "slack-connector", "fact_id": "slack-connector.limits.max-rows", "fact_v": 1},
        {"feature": "slack-connector", "fact_id": "slack-connector.other.fact", "fact_v": 2},
    ],
)
def test_is_current_rejects_anything_but_the_current_version(meta):
    c = cat.set_facts(
        _with_slack(), "slack-connector", {"slack-connector.limits.max-rows": 2}, at=AT, source="x"
    )
    assert not cat.is_current(c, meta)


def test_withdrawn_feature_hides_all_its_facts():
    fid = "slack-connector.limits.max-rows"
    c = cat.set_facts(_with_slack(), "slack-connector", {fid: 1}, at=AT, source="x")
    c = cat.set_state(c, "slack-connector", "withdrawn")
    assert not cat.is_current(c, {"feature": "slack-connector", "fact_id": fid, "fact_v": 1})
    assert cat.listing(c) == [] and cat.find(c, "slack connector") == []


def test_find_by_name_alias_and_longest_match():
    c = cat.upsert_feature(_with_slack(), "connector", name="Connector", summary="All of them.")
    assert [m["slug"] for m in cat.find(c, "How does the Slack connector post?")] == [
        "slack-connector",
        "connector",
    ]
    assert cat.find(c, "tell me about the slack integration")[0]["slug"] == "slack-connector"
    assert cat.find(c, "slack")[0]["slug"] == "slack-connector"  # partial name typed
    assert cat.find(c, "canvas") == []


def test_load_and_save_round_trip(store):
    assert cat.load()["features"] == {}
    saved = cat.save(_with_slack(), at=AT)
    assert saved["updated_at"] == AT
    assert cat.load() == saved
    assert cat.CATALOG_KEY in store
