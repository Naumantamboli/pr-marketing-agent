import copy
import json
from pathlib import Path

import pytest

from utils import catalog, storage

SEED = json.loads((Path(__file__).resolve().parents[1] / "seed" / "catalog_seed.json").read_text())


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_OUTPUT", "true")
    monkeypatch.setattr(storage, "_LOCAL_DIR", tmp_path)


def test_seed_is_consistent():
    assert catalog.check_seed(SEED) == []
    for c in SEED["architecture"]["components"]:
        catalog.check_slug(c["slug"])
        assert c["role"]
    for f in SEED["features"]:
        catalog.check_slug(f["slug"])
        assert f["status"] in catalog.STATUSES
        assert set(f["overview"]) == set(catalog.OVERVIEW_FIELDS)
        for field in ("what_it_is", "who_its_for", "how_it_works"):
            assert f["overview"][field], f"{f['slug']} has no {field}"


def test_check_seed_catches_bad_references():
    bad = copy.deepcopy(SEED)
    bad["features"][0]["components"].append("ghost")
    bad["architecture"]["components"][0]["depends_on"].append("phantom")
    bad["features"][1]["sources"] = []
    problems = catalog.check_seed(bad)
    assert any("unknown component 'ghost'" in p for p in problems)
    assert any("unknown 'phantom'" in p for p in problems)
    assert any("no sources" in p for p in problems)
    with pytest.raises(ValueError):
        catalog.apply_seed(bad)


def test_apply_seed_writes_everything(store):
    index = catalog.apply_seed(SEED)
    assert index["baseline"] == {"as_of": SEED["as_of"], "source": SEED["source"]}
    assert [f["slug"] for f in index["features"]] == [f["slug"] for f in SEED["features"]]
    assert len(index["architecture"]["components"]) == len(SEED["architecture"]["components"])
    canvas = catalog.load_feature("canvas")
    assert canvas["overview"]["what_it_is"].startswith("A drag-and-drop editor")
    assert canvas["updates"] == []
    assert catalog.find_feature(catalog.load_index(), "visual builder")["slug"] == "canvas"


def test_reapplying_seed_keeps_updates_and_merges_sources(store):
    catalog.apply_seed(SEED)
    catalog.add_feature_update(
        "canvas", "Export supports two loops", "changed", "2026-12-01", ["org/repo#9"]
    )
    catalog.apply_seed(SEED)
    canvas = catalog.load_feature("canvas")
    assert [u["summary"] for u in canvas["updates"]] == ["Export supports two loops"]
    assert "org/repo#9" in canvas["sources"]
    assert catalog.load_index()["features"][1]["last_update"] == "2026-12-01"


def test_rendered_seed_has_all_sections(store):
    catalog.apply_seed(SEED)
    page = catalog.render()
    assert page.index("## Overview") < page.index("## Architecture") < page.index("## Features")
    for f in SEED["features"]:
        assert f"### {f['name']} (shipped)" in page
