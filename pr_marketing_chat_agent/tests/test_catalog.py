"""The chat agent reading the catalog that ProductCatalogUpdater writes."""

import json
from datetime import date

import pytest

from tools import activities
from utils import catalog_reader, storage

PREFIX = "pr_marketing_v2/knowledge"
INDEX = {
    "product": "aetherion",
    "updated_at": "2026-11-12T10:00:00+00:00",
    "baseline": {"as_of": "2026-11-12", "source": "https://calfus.dev.aetherion.io/docs"},
    "overview": "Aetherion builds and runs AI agents.",
    "architecture": {
        "summary": "Durable workflows.",
        "components": [{"slug": "workers", "name": "Workers", "role": "Run agents and tools"}],
        "last_update": "2026-11-05",
    },
    "features": [
        {
            "slug": "canvas",
            "name": "Canvas",
            "summary": "Visual builder.",
            "status": "shipped",
            "aliases": ["visual builder"],
            "last_update": "2026-11-10",
        },
        {
            "slug": "evaluations",
            "name": "Evaluations",
            "summary": "Scored test runs.",
            "status": "in_progress",
            "aliases": ["evals"],
            "last_update": None,
        },
    ],
}
CANVAS = {
    "slug": "canvas",
    "name": "Canvas",
    "summary": "Visual builder.",
    "status": "shipped",
    "aliases": ["visual builder"],
    "components": ["workers", "gone"],
    "overview": {
        "what_it_is": "A graph editor.",
        "who_its_for": "",
        "how_it_works": "Nodes.",
        "limitations": "Two loops per canvas.",
    },
    "sources": ["https://calfus.dev.aetherion.io/docs/canvas", "https://gh/12"],
    "updates": [
        {"date": "2026-09-01", "kind": "added", "summary": "Run history.", "sources": []},
        {
            "date": "2026-11-10",
            "kind": "changed",
            "summary": "Two loops.",
            "sources": ["https://gh/12"],
            "event_id": "pr:org/app#12",
            "recorded_at": "x",
        },
    ],
    "field_meta": {"overview.limitations": {"updated_at": "2026-11-10T09:00:00+00:00"}},
    "updated_at": "2026-11-10T09:00:00+00:00",
}
ARCH = {
    "summary": "Durable workflows.",
    "components": [],
    "updates": [
        {
            "date": "2026-11-05",
            "kind": "changed",
            "summary": "Workers autoscale.",
            "component": "workers",
            "sources": [],
        }
    ],
}


@pytest.fixture
def catalog_store(local_output, monkeypatch):
    monkeypatch.setenv("TENANT_ID", "tenant")
    monkeypatch.delenv("CATALOG_BUCKET", raising=False)
    for rel, value in {
        "catalog.json": INDEX,
        "features/canvas.json": CANVAS,
        "architecture.json": ARCH,
    }.items():
        path = local_output / PREFIX / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    return local_output


def test_bucket_prefers_catalog_bucket_then_tenant(monkeypatch):
    monkeypatch.setenv("CATALOG_BUCKET", "shared")
    assert catalog_reader.bucket("cust") == "shared"
    monkeypatch.delenv("CATALOG_BUCKET")
    monkeypatch.setenv("TENANT_ID", "tenant")
    assert catalog_reader.bucket("cust") == "tenant"
    monkeypatch.delenv("TENANT_ID")
    assert catalog_reader.bucket("cust") == "cust"


def test_overview_lists_every_feature_with_its_stage(catalog_store):
    out = catalog_reader.overview("tenant")
    assert out["overview"].startswith("Aetherion")
    assert [(f["slug"], f["stage"]) for f in out["features"]] == [
        ("canvas", "Shipped"),
        ("evaluations", "In progress"),
    ]
    assert out["as_of"] == "2026-11-12"


def test_feature_by_alias_with_updates_newest_first(catalog_store):
    f = catalog_reader.feature("tenant", "Visual Builder")
    assert f["slug"] == "canvas" and f["stage"] == "Shipped"
    assert [u["summary"] for u in f["recent_updates"]] == ["Two loops.", "Run history."]
    assert "event_id" not in f["recent_updates"][0]
    assert f["components"] == [
        {"slug": "workers", "name": "Workers", "role": "Run agents and tools"}
    ]
    assert f["last_edited"] == "2026-11-10"
    with pytest.raises(ValueError, match="Known features: canvas, evaluations"):
        catalog_reader.feature("tenant", "billing")


def test_recent_changes_across_features_and_architecture(catalog_store):
    out = catalog_reader.recent_changes("tenant", 30, today=date(2026, 11, 12))
    assert [(c["feature"], c["summary"]) for c in out["changes"]] == [
        ("canvas", "Two loops."),
        ("architecture", "Workers autoscale."),
    ]
    assert out["since"] == "2026-10-13"


def test_missing_catalog_tells_the_model_to_fall_back(local_output, monkeypatch):
    monkeypatch.setenv("TENANT_ID", "tenant")
    with pytest.raises(catalog_reader.CatalogMissing, match="docs and Jira"):
        catalog_reader.overview("tenant")


async def test_tools_and_catalog_sources_for_drafts(catalog_store):
    overview = await activities.catalog_overview_tool({"customer_id": "cust"})
    assert overview["features"][0]["name"] == "Canvas"
    feature = await activities.catalog_feature_tool({"feature": "canvas"})
    assert feature["overview"]["limitations"] == "Two loops per canvas."
    changes = await activities.catalog_changes_tool({"days": "9999"})
    assert changes["days"] == 365

    sources, errors = await activities._fetch_sources(["catalog:canvas", "catalog:billing"])
    assert [s["ref"] for s in sources] == ["catalog:canvas"]
    assert "Limitations: Two loops per canvas." in sources[0]["text"]
    assert "2026-11-10 (changed): Two loops." in sources[0]["text"]
    assert errors and errors[0].startswith("catalog:billing")
    assert storage.get_json("tenant", f"{PREFIX}/catalog.json")["product"] == "aetherion"
