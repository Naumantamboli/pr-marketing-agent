import json

import pytest

from utils import catalog, storage


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Local storage in a temp dir; returns a reader for the saved JSON."""
    monkeypatch.setenv("LOCAL_OUTPUT", "true")
    monkeypatch.setattr(storage, "_LOCAL_DIR", tmp_path)
    return lambda key: json.loads((tmp_path / key).read_text())


# --- pure helpers ----------------------------------------------------------------------------


def test_slugify_and_check_slug():
    assert catalog.slugify("Slack Connector (v2)") == "slack-connector-v2"
    with pytest.raises(ValueError):
        catalog.slugify("!!!")
    with pytest.raises(ValueError):
        catalog.check_slug("Bad Slug")


def test_make_update_validates_and_cleans():
    u = catalog.make_update(" Added retries ", "added", "2026-10-02", ["pr#1", " ", "pr#1"])
    assert {k: u[k] for k in ("date", "kind", "summary", "sources")} == {
        "date": "2026-10-02",
        "kind": "added",
        "summary": "Added retries",
        "sources": ["pr#1"],
    }
    assert u["id"].startswith("u-") and u["recorded_at"]
    for bad in (
        dict(summary="x", kind="improved", on="2026-10-02"),
        dict(summary=" ", kind="added", on="2026-10-02"),
        dict(summary="x", kind="added", on="02/10/2026"),
    ):
        with pytest.raises(ValueError):
            catalog.make_update(**bad)


def test_apply_update_sorts_and_replaces_same_source():
    f = catalog.new_feature("Workflow Builder")
    f = catalog.apply_update(f, catalog.make_update("later", "added", "2026-10-05", ["pr#2"]))
    f = catalog.apply_update(f, catalog.make_update("first", "added", "2026-10-01", ["pr#1"]))
    f = catalog.apply_update(
        f, catalog.make_update("first, again", "changed", "2026-10-01", ["pr#1"])
    )
    assert [u["summary"] for u in f["updates"]] == ["first, again", "later"]


def test_updates_without_sources_never_replace_each_other():
    f = catalog.new_feature("X")
    for _ in range(2):
        f = catalog.apply_update(f, catalog.make_update("manual note", "changed", "2026-10-01"))
    assert len(f["updates"]) == 2


def test_same_pr_on_two_components_keeps_both():
    a = catalog.empty_architecture()
    a = catalog.apply_update(a, catalog.make_update("q", "added", "2026-10-01", ["pr#9"], "queue"))
    a = catalog.apply_update(a, catalog.make_update("g", "added", "2026-10-01", ["pr#9"], "gw"))
    assert len(a["updates"]) == 2


def test_edit_feature_merges_overview_and_sources():
    f = catalog.new_feature("X")
    f = catalog.edit_feature(f, overview={"what_it_is": "A"}, sources=["doc.md"])
    f = catalog.edit_feature(f, overview={"limitations": "B"}, sources=["AP-1", "doc.md"])
    assert f["overview"]["what_it_is"] == "A" and f["overview"]["limitations"] == "B"
    assert f["sources"] == ["doc.md", "AP-1"]
    with pytest.raises(ValueError):
        catalog.edit_feature(f, overview={"pricing": "C"})
    with pytest.raises(ValueError):
        catalog.edit_feature(f, status="live")


def test_component_upsert_keeps_order_and_drops_self_dependency():
    a = catalog.empty_architecture()
    a = catalog.upsert_component_record(a, "Temporal workers", "Run workflows")
    a = catalog.upsert_component_record(a, "Object storage", "Files")
    a = catalog.upsert_component_record(
        a, "Temporal workers", depends_on=["object-storage", "temporal-workers"]
    )
    assert [c["slug"] for c in a["components"]] == ["temporal-workers", "object-storage"]
    assert a["components"][0] == {
        "slug": "temporal-workers",
        "name": "Temporal workers",
        "role": "Run workflows",
        "depends_on": ["object-storage"],
    }


def test_find_feature_by_name_slug_or_alias():
    f = catalog.edit_feature(catalog.new_feature("Workflow Builder"), aliases=["Pipelines"])
    index = catalog.with_feature(catalog.empty_index(), f)
    for text in ("workflow-builder", "Workflow builder", "pipelines"):
        assert catalog.find_feature(index, text)["slug"] == "workflow-builder"
    assert catalog.find_feature(index, "slack") is None


# --- storage ---------------------------------------------------------------------------------


def test_empty_store_loads_empty_records(store):
    assert catalog.load_index()["features"] == []
    assert catalog.load_architecture()["components"] == []
    assert catalog.load_feature("nope") is None


def test_feature_writes_detail_file_and_index_entry(store):
    catalog.upsert_feature(
        "Workflow Builder",
        summary="Build agent flows",
        aliases=["flows"],
        overview={"what_it_is": "A visual editor"},
    )
    catalog.add_feature_update(
        "workflow-builder",
        "Retries on failed steps",
        "added",
        "2026-10-02",
        ["org/repo#412", "AP-131"],
    )

    detail = store("pr_marketing_v2/knowledge/features/workflow-builder.json")
    assert detail["overview"]["what_it_is"] == "A visual editor"
    assert detail["sources"] == ["org/repo#412", "AP-131"]
    assert detail["updates"][0]["summary"] == "Retries on failed steps"

    index = store("pr_marketing_v2/knowledge/catalog.json")
    assert index["features"] == [
        {
            "slug": "workflow-builder",
            "name": "Workflow Builder",
            "summary": "Build agent flows",
            "status": "shipped",
            "aliases": ["flows"],
            "last_update": "2026-10-02",
        }
    ]


def test_rename_keeps_slug_and_single_index_entry(store):
    catalog.upsert_feature("Workflow Builder")
    catalog.upsert_feature("Flow Studio", slug="workflow-builder", status="in_progress")
    index = catalog.load_index()
    assert [(f["slug"], f["name"], f["status"]) for f in index["features"]] == [
        ("workflow-builder", "Flow Studio", "in_progress")
    ]


def test_update_for_unknown_feature_or_component_fails(store):
    with pytest.raises(ValueError, match="No feature"):
        catalog.add_feature_update("ghost", "x", "added", "2026-10-02")
    with pytest.raises(ValueError, match="No component"):
        catalog.add_architecture_update("x", "added", "2026-10-02", "ghost")


def test_architecture_flows_into_index(store):
    catalog.set_architecture_summary("Agents run as Temporal workflows.")
    catalog.upsert_component("LLM gateway", "Routes model calls", depends_on=["object-storage"])
    catalog.add_architecture_update(
        "All model calls go through the gateway",
        "added",
        "2026-10-20",
        "llm-gateway",
        ["org/repo#455"],
    )
    index = catalog.load_index()["architecture"]
    assert index == {
        "summary": "Agents run as Temporal workflows.",
        "components": [
            {"slug": "llm-gateway", "name": "LLM gateway", "role": "Routes model calls"}
        ],
        "last_update": "2026-10-20",
    }
    assert catalog.load_architecture()["components"][0]["depends_on"] == ["object-storage"]


def test_overview_and_feature_writes_do_not_clobber_each_other(store):
    catalog.set_overview("Aetherion is an agent platform.")
    catalog.upsert_feature("Workflow Builder")
    catalog.set_architecture_summary("Temporal.")
    index = catalog.load_index()
    assert index["overview"] == "Aetherion is an agent platform."
    assert index["architecture"]["summary"] == "Temporal."
    assert len(index["features"]) == 1


def test_remove_feature_drops_index_entry_only(store):
    catalog.upsert_feature("Old Thing")
    catalog.remove_feature("old-thing")
    assert catalog.load_index()["features"] == []
    assert catalog.load_feature("old-thing") is not None
    with pytest.raises(ValueError):
        catalog.remove_feature("old-thing")


def test_render_shows_every_section(store):
    catalog.set_overview("Aetherion is an agent platform.")
    catalog.set_architecture_summary("Agents run as Temporal workflows.")
    catalog.upsert_component("LLM gateway", "Routes model calls")
    catalog.add_architecture_update("Gateway added", "added", "2026-10-20", "llm-gateway")
    catalog.upsert_feature(
        "Workflow Builder", summary="Build agent flows", overview={"what_it_is": "A visual editor"}
    )
    catalog.add_feature_update("workflow-builder", "Retries", "added", "2026-10-02", ["pr#1"])

    page = catalog.render()
    for text in (
        "## Overview",
        "Aetherion is an agent platform.",
        "## Architecture",
        "**LLM gateway** (`llm-gateway`): Routes model calls",
        "- **2026-10-20** added [llm-gateway]: Gateway added",
        "## Features",
        "### Workflow Builder (shipped)",
        "**What it is:** A visual editor",
        "- **2026-10-02** added: Retries (pr#1)",
    ):
        assert text in page
    assert page.index("## Overview") < page.index("## Architecture") < page.index("## Features")


# --- revisions, history, provenance ----------------------------------------------------------


def test_same_event_replaces_its_update_even_with_new_wording():
    f = catalog.new_feature("X")
    f = catalog.apply_update(
        f, catalog.make_update("v1", "added", "2026-10-01", ["a"], event_id="pr:r#1")
    )
    f = catalog.apply_update(
        f, catalog.make_update("v2", "added", "2026-10-01", ["b"], event_id="pr:r#1")
    )
    assert [u["summary"] for u in f["updates"]] == ["v2"]
    assert catalog.has_update_from(f, "pr:r#1")
    assert not catalog.has_update_from(f, "pr:r#2")


def test_field_meta_records_only_changed_fields():
    f = catalog.new_feature("X")
    f = catalog.edit_feature(f, summary="S", overview={"what_it_is": "W"}, provenance=["pr:r#1"])
    assert f["field_meta"]["summary"]["sources"] == ["pr:r#1"]
    assert f["field_meta"]["overview.what_it_is"]["sources"] == ["pr:r#1"]
    f = catalog.edit_feature(f, summary="S", overview={"limitations": "L"}, provenance=["pr:r#2"])
    assert f["field_meta"]["summary"]["sources"] == ["pr:r#1"]  # unchanged value, meta kept
    assert f["field_meta"]["overview.limitations"]["sources"] == ["pr:r#2"]
    assert catalog.get_field(f, "overview.limitations") == "L"
    with pytest.raises(ValueError):
        catalog.get_field(f, "pricing")


def test_every_write_bumps_revision_and_keeps_history(store):
    catalog.upsert_feature("Workflow Builder", summary="v1")
    catalog.upsert_feature("Workflow Builder", summary="v2")
    catalog.upsert_feature("Workflow Builder", summary="v3")
    f = catalog.load_feature("workflow-builder")
    assert f["revision"] == 3
    key = catalog.feature_key("workflow-builder")
    assert catalog.load_history(key, 1)["summary"] == "v1"
    assert catalog.load_history(key, 2)["summary"] == "v2"


def test_restore_rolls_back_as_a_new_revision(store):
    catalog.upsert_feature("Workflow Builder", summary="good")
    catalog.upsert_feature("Workflow Builder", summary="bad")
    restored = catalog.restore_feature("workflow-builder", 1)
    assert restored["summary"] == "good" and restored["revision"] == 3
    assert catalog.load_index()["features"][0]["summary"] == "good"


def test_stale_write_is_refused(store):
    catalog.upsert_feature("Workflow Builder", summary="v1")
    seen = catalog.load_feature("workflow-builder")
    catalog.upsert_feature("Workflow Builder", summary="someone else")
    with pytest.raises(catalog.StaleRecordError):
        catalog.commit(features=[catalog.edit_feature(seen, summary="mine")])


def test_overview_change_keeps_previous_index(store):
    catalog.set_overview("first")
    rev = catalog.load_index()["revision"]
    catalog.set_overview("second")
    assert catalog.load_history(catalog.INDEX_KEY, rev)["overview"] == "first"
