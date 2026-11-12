"""Pure parts of the catalog updater: quotes, events, docs diff, routing, policy."""

import pytest

from utils import catalog, docs_site, events, policy, routing
from utils.quotes import normalize, quote_in

# --- quotes ----------------------------------------------------------------------------------


def test_quote_matching_tolerates_formatting_only():
    text = "Export is **all or nothing**, limited to one\nloop per canvas — “for now”."
    assert quote_in("all or nothing, limited to one loop per canvas", text)
    assert quote_in('limited to one loop per canvas - "for now"', text)
    assert not quote_in("limited to two loops per canvas", text)
    assert not quote_in("one loop", text)  # too short to mean anything
    assert normalize("  A B  ") == "a b"


# --- events ----------------------------------------------------------------------------------


def _pr(**over):
    pr = {
        "url": "https://github.com/org/app/pull/12",
        "repo": "org/app",
        "number": 12,
        "title": "[AP-7] feat(canvas): allow two loops in export",
        "body": "Export now supports two loops. token=supersecretvalue",
        "head_branch": "feat/AP-7-two-loops",
        "labels": ["canvas"],
        "commits": ["feat: two loops"],
        "merged_at": "2026-11-10T09:00:00Z",
        "type_check": {"decision": "include"},
        "feature_doc": {"status": "missing"},
    }
    return {**pr, **over}


def test_pr_event_shape_and_redaction():
    issue = {
        "key": "AP-7",
        "summary": "Two loops",
        "type": "Story",
        "status": "Done",
        "description": "Allow a second loop.",
        "url": "https://jira/browse/AP-7",
    }
    ev = events.pr_event(_pr(), [issue])
    assert ev["event_id"] == "pr:org/app#12"
    assert ev["occurred_at"] == "2026-11-10"
    assert [e["id"] for e in ev["evidence"]] == ["e1", "e2"]
    assert "supersecretvalue" not in ev["evidence"][0]["text"]
    assert ev["evidence"][1]["source"] == "jira:AP-7"
    assert ev["refs"]["jira"] == ["AP-7"]


def test_jira_keys_from_title_branch_and_body():
    keys = events.jira_keys("[AP-7] x", "feat/ap-8-y", "see AP-7, OTHER-1", projects=["AP"])
    assert keys == ["AP-7", "AP-8"]


def test_event_round_trip(local_store):
    ev = events.pr_event(_pr())
    key = events.save_event(ev)
    assert events.load_event(key) == ev
    with pytest.raises(ValueError):
        events.load_event("nope.json")


# --- docs ------------------------------------------------------------------------------------

FULL = """# Aetherion SDK

---

## Canvas - Visual Agent Builder

**URL:** https://docs.aetherion.ai/canvas
**Description:** Build an agent as a graph.

Canvas is the visual builder.

One loop per canvas.

---

## Aetherion

**URL:** https://docs.aetherion.ai/
**Description:** Home

Build agents.
"""


def test_split_pages_maps_urls_to_the_docs_site(monkeypatch):
    monkeypatch.setenv("DOCS_SITE_URL", "https://calfus.dev.aetherion.io/docs")
    pages = docs_site.split_pages(FULL)
    assert set(pages) == {"canvas", "index"}
    assert pages["canvas"]["url"] == "https://calfus.dev.aetherion.io/docs/canvas"
    assert pages["canvas"]["text"] == "Canvas is the visual builder.\n\nOne loop per canvas."


def test_diff_reports_only_what_changed():
    old = docs_site.split_pages(FULL)
    new = docs_site.split_pages(
        FULL.replace("One loop per canvas.", "Two loops per canvas.").replace(
            "## Aetherion\n", "## Home\n"
        )
    )
    changes = docs_site.diff_snapshots(old, new)
    canvas = next(c for c in changes if c["slug"] == "canvas")
    assert canvas["added"] == ["Two loops per canvas."]
    assert canvas["removed"] == ["One loop per canvas."]
    ev = events.docs_event(canvas, "2026-11-12")
    assert ev["kind"] == "docs_changed" and ev["refs"]["page_status"] == "changed"
    assert "Canvas is the visual builder" not in ev["evidence"][0]["text"]


def test_removed_page_and_snapshot_staging(local_store):
    old = docs_site.split_pages(FULL)
    new = {k: v for k, v in old.items() if k != "canvas"}
    [gone] = docs_site.diff_snapshots(old, new)
    assert gone["status"] == "removed" and gone["hash"] is None

    docs_site.stage_snapshot(new)
    assert docs_site.load_snapshot() is None
    assert docs_site.promote_snapshot()
    assert set(docs_site.load_snapshot()) == {"index"}
    assert not docs_site.promote_snapshot()  # nothing staged any more
    assert docs_site.load_snapshot(shadow=True) is None


# --- routing ---------------------------------------------------------------------------------


def test_routing_by_text_alias_and_docs_page(seeded):
    index = catalog.load_index()
    features = catalog.load_features(f["slug"] for f in index["features"])
    ev = events.pr_event(_pr(body="The visual builder now exports two loops."))
    assert "canvas" in routing.feature_hits(ev, index)

    page = "https://calfus.dev.aetherion.io/docs/evals"
    docs_ev = {"title": "Docs: Evaluations", "evidence": [], "refs": {"docs_page": page}}
    assert routing.page_hits(docs_ev, features) == ["evaluations"]

    merged = routing.merge(["a"], ["b", "a"], ["c"], known={"a", "b"}, limit=4)
    assert merged == ["a", "b"]


# --- policy ----------------------------------------------------------------------------------

CLAIMS = {"c1": {"id": "c1"}}
FEATURES = {"canvas"}
COMPONENTS = {"workers"}


def _op(**kw):
    return policy.normalize(
        {
            "op": "add_update",
            "target": "canvas",
            "claim_ids": ["c1"],
            "kind": "changed",
            "summary": "Two loops.",
            **kw,
        }
    )


def test_validate_catches_bad_operations():
    assert policy.validate(_op(), CLAIMS, FEATURES, COMPONENTS) == []
    assert "cites no claims" in policy.validate(_op(claim_ids=[]), CLAIMS, FEATURES, COMPONENTS)
    assert policy.validate(_op(claim_ids=["c9"]), CLAIMS, FEATURES, COMPONENTS)
    assert policy.validate(_op(target="ghost"), CLAIMS, FEATURES, COMPONENTS)
    edit = _op(op="edit_field", field="name", text="X")
    assert any("can't be edited" in p for p in policy.validate(edit, CLAIMS, FEATURES, COMPONENTS))
    new = _op(op="new_feature", name="Canvas", summary="dup")
    assert any("already exists" in p for p in policy.validate(new, CLAIMS, FEATURES, COMPONENTS))
    comp = _op(
        op="upsert_component", target="", name="Queue", role="Holds work", depends_on=["ghost"]
    )
    assert comp["target"] == "queue" and comp["target_type"] == "component"
    assert policy.validate(comp, CLAIMS, FEATURES, COMPONENTS)


def test_status_rules_follow_the_evidence():
    pr_ev = {"kind": "pr_merged", "refs": {}}
    docs_ev = {"kind": "docs_changed", "refs": {"page_status": "added"}}
    status = _op(op="set_status", status="shipped")
    assert policy.apply_status_rules(status, pr_ev, {"status": "in_progress"})[0] is None
    assert policy.apply_status_rules(status, docs_ev, {"status": "in_progress"})[0] == status
    assert policy.apply_status_rules(status, docs_ev, {"status": "shipped"})[0] is None
    planned = _op(op="set_status", status="planned")
    assert policy.apply_status_rules(planned, docs_ev, {"status": "shipped"})[0] is None
    new = _op(op="new_feature", name="Thing", summary="s")
    assert policy.apply_status_rules(new, pr_ev, None)[0]["status"] == "in_progress"
    assert policy.apply_status_rules(new, docs_ev, None)[0]["status"] == "shipped"


def test_tiers():
    assert policy.tier(_op()) == "A"
    assert policy.tier(_op(kind="deprecated")) == "B"
    assert policy.tier(_op(op="edit_field", field="summary", text="x")) == "B"
    assert policy.tier(_op(op="set_status", status="shipped")) == "B"
    assert policy.tier(_op(op="set_status", status="deprecated")) == "C"
    assert policy.tier(_op(op="new_feature", name="X", summary="s")) == "C"


def test_needs_refresh_counts_updates_since_last_overview_edit():
    f = catalog.edit_feature(catalog.new_feature("X"), overview={"what_it_is": "W"})
    for n in range(4):
        f = catalog.apply_update(
            f, catalog.make_update(f"u{n}", "added", "2026-11-01", event_id=f"pr:r#{n}")
        )
    assert not policy.needs_refresh(f)
    f = catalog.apply_update(f, catalog.make_update("u5", "added", "2026-11-01", event_id="pr:r#5"))
    assert policy.needs_refresh(f)
