"""Applying proposals to the seeded catalog: every op type, stale checks, idempotency."""

from utils import catalog, policy, proposals

EVENT = {
    "event_id": "pr:org/app#12",
    "kind": "pr_merged",
    "occurred_at": "2026-11-10",
    "title": "feat(canvas): two loops",
    "url": "https://github.com/org/app/pull/12",
    "refs": {"jira": ["AP-7"]},
    "evidence": [],
}
CLAIMS = {
    "c1": {
        "id": "c1",
        "statement": "Export supports two loops.",
        "evidence_id": "e1",
        "quote": "Export now supports two loops",
        "about": "canvas",
        "source": "pr",
    }
}


def _proposal(**op):
    op = policy.normalize({"claim_ids": ["c1"], **op})
    feature = catalog.load_feature(op["target"]) if op.get("target_type") != "component" else None
    return proposals.make_proposal(
        EVENT, op, CLAIMS, policy.tier(op), feature, catalog.load_architecture()
    )


def test_add_update_lands_once_with_sources(seeded):
    p = _proposal(op="add_update", target="canvas", kind="changed", summary="Two loops.")
    assert p["tier"] == "A" and p["preview"]["after"] == "[changed] Two loops."
    [done] = proposals.apply([p])
    assert done["status"] == "applied"
    canvas = catalog.load_feature("canvas")
    [u] = canvas["updates"]
    assert u["event_id"] == "pr:org/app#12" and u["date"] == "2026-11-10"
    assert u["sources"] == ["https://github.com/org/app/pull/12", "AP-7"]
    assert "AP-7" in canvas["sources"]
    assert catalog.load_index()["features"][1]["last_update"] == "2026-11-10"

    proposals.apply([p])  # same event again: replaced, not duplicated
    assert len(catalog.load_feature("canvas")["updates"]) == 1


def test_edit_field_keeps_history_and_provenance(seeded):
    before = catalog.load_feature("canvas")
    p = _proposal(
        op="edit_field",
        target="canvas",
        field="overview.limitations",
        text="Export allows two loops per canvas.",
    )
    assert p["expected"] == before["overview"]["limitations"]
    assert p["preview"]["before"] == before["overview"]["limitations"]
    [done] = proposals.apply([p])
    assert done["status"] == "applied"
    after = catalog.load_feature("canvas")
    assert after["overview"]["limitations"] == "Export allows two loops per canvas."
    assert after["field_meta"]["overview.limitations"]["sources"] == [EVENT["url"]]
    old = catalog.load_history(catalog.feature_key("canvas"), before["revision"])
    assert old["overview"]["limitations"] == before["overview"]["limitations"]


def test_stale_proposal_is_not_applied(seeded):
    p = _proposal(op="edit_field", target="canvas", field="summary", text="New summary.")
    catalog.upsert_feature("Canvas", slug="canvas", summary="Someone changed it.")
    [done] = proposals.apply([p])
    assert done["status"] == "stale"
    assert catalog.load_feature("canvas")["summary"] == "Someone changed it."


def test_new_feature_component_and_status(seeded):
    new = _proposal(
        op="new_feature",
        name="Agent Marketplace",
        summary="Browse agents.",
        overview={"what_it_is": "A catalog of agents."},
        components=["web-app"],
        status="in_progress",
    )
    comp = _proposal(
        op="upsert_component",
        target_type="component",
        name="Event bus",
        role="Carries trigger events.",
        depends_on=["workers"],
    )
    upd = _proposal(
        op="add_update",
        target_type="component",
        target="workers",
        kind="changed",
        summary="Workers scale on queue depth.",
    )
    status = _proposal(op="set_status", target="evaluations", status="deprecated")
    done = proposals.apply([new, comp, upd, status])
    assert [d["status"] for d in done] == ["applied"] * 4

    feature = catalog.load_feature("agent-marketplace")
    assert feature["status"] == "in_progress" and feature["components"] == ["web-app"]
    arch = catalog.load_architecture()
    assert catalog.find_component(arch, "event-bus")["depends_on"] == ["workers"]
    assert arch["updates"][0]["component"] == "workers"
    assert catalog.load_feature("evaluations")["status"] == "deprecated"
    index = catalog.load_index()
    assert any(f["slug"] == "agent-marketplace" for f in index["features"])
    assert index["architecture"]["last_update"] == "2026-11-10"

    [again] = proposals.apply([new])
    assert again["status"] == "stale"  # never overwrite an existing feature


def test_queue_and_take(seeded):
    a = _proposal(op="add_update", target="canvas", kind="changed", summary="Two loops.")
    b = _proposal(op="edit_field", target="canvas", field="summary", text="S.")
    proposals.queue([a, b])
    proposals.queue([a])  # already queued: no duplicate
    assert [p["id"] for p in proposals.load_pending()] == [a["id"], b["id"]]
    assert [p["id"] for p in proposals.take([b["id"]])] == [b["id"]]
    assert [p["id"] for p in proposals.load_pending()] == [a["id"]]
