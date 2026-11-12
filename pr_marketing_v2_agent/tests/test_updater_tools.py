"""The catalog updater's tools end to end on the seeded catalog, with a scripted LLM."""

import pytest

from tools import apply as apply_tools
from tools import understand
from utils import catalog, events, llm, proposals, storage
from utils.schemas import (
    CheckResult,
    ComposeResult,
    ExtractResult,
    RouteResult,
    VerifyResult,
)


def _fn(tool):
    return getattr(tool, "fn", tool)


PR = {
    "url": "https://github.com/org/app/pull/12",
    "repo": "org/app",
    "number": 12,
    "title": "feat(canvas): export supports two loops",
    "body": "Canvas export now supports two loops per canvas. Nested loops are still refused.",
    "head_branch": "feat/two-loops",
    "labels": [],
    "commits": [],
    "merged_at": "2026-11-10T09:00:00Z",
    "feature_doc": {"status": "missing"},
}


@pytest.fixture
def scripted(monkeypatch):
    """Replace llm.structured with canned answers per step; records the prompts."""
    answers, prompts_seen = {}, {}

    async def fake(step, system, prompt, schema):
        prompts_seen[step] = prompt
        result = answers[step]
        assert isinstance(result, schema)
        return result

    monkeypatch.setattr(llm, "structured", fake)
    return answers, prompts_seen


@pytest.fixture
def event_key(seeded):
    return events.save_event(events.pr_event(PR))


async def test_pipeline_keeps_only_what_code_can_verify(seeded, event_key, scripted):
    answers, seen = scripted
    answers["route"] = RouteResult(product_change=True, features=["canvas", "ghost"])
    route = await _fn(understand.route_event_tool)(event_key)
    assert route["features"][0] == "canvas" and "ghost" not in route["features"]
    assert "canvas" in seen["route"]  # deterministic hint was offered

    answers["extract"] = ExtractResult(
        claims=[
            {
                "id": "x",
                "statement": "Export supports two loops per canvas.",
                "evidence_id": "e1",
                "quote": "export now supports two loops per canvas",
                "about": "canvas",
            },
            {
                "id": "y",
                "statement": "Export supports ten loops.",
                "evidence_id": "e1",
                "quote": "export supports ten loops",
                "about": "canvas",
            },
            {
                "id": "z",
                "statement": "Something else.",
                "evidence_id": "e1",
                "quote": "Nested loops are still refused",
                "about": "billing",
            },
        ],
        gaps=["Unclear whether loops can be nested in the UI"],
    )
    ex = await _fn(understand.extract_claims_tool)(event_key, route)
    assert [c["id"] for c in ex["claims"]] == ["c1"]
    assert {d["reason"] for d in ex["dropped"]} == {
        "quote is not in the evidence",
        "about unknown subject 'billing'",
    }
    assert ex["gaps"]

    answers["verify"] = VerifyResult(verdicts=[{"claim_id": "c1", "supported": True}])
    ve = await _fn(understand.verify_claims_tool)(event_key, ex["claims"])
    assert [c["id"] for c in ve["claims"]] == ["c1"]

    answers["compose"] = ComposeResult(
        ops=[
            {
                "op": "add_update",
                "target": "canvas",
                "claim_ids": ["c1"],
                "kind": "changed",
                "summary": "Export now supports two loops per canvas.",
            },
            {
                "op": "edit_field",
                "target": "canvas",
                "claim_ids": ["c1"],
                "field": "overview.limitations",
                "text": "Export allows two loops per canvas and is fast.",
            },
            {"op": "set_status", "target": "canvas", "claim_ids": ["c1"], "status": "shipped"},
            {
                "op": "add_update",
                "target": "canvas",
                "claim_ids": ["c9"],
                "kind": "added",
                "summary": "Made up.",
            },
            {"op": "no_change", "reason": "nothing else"},
        ]
    )
    co = await _fn(understand.compose_ops_tool)(event_key, route, ve["claims"])

    answers["check"] = CheckResult(
        checks=[
            {"index": 0, "verdict": "supported"},
            {
                "index": 1,
                "verdict": "narrowed",
                "revised_text": "Export allows two loops per canvas.",
                "reason": "'fast' isn't stated",
            },
        ]
    )
    ch = await _fn(understand.check_ops_tool)(event_key, co["ops"], ve["claims"])
    tiers = {p["op"]["op"]: p["tier"] for p in ch["proposals"]}
    assert tiers == {"add_update": "A", "edit_field": "B"}
    edit = next(p for p in ch["proposals"] if p["op"]["op"] == "edit_field")
    assert edit["op"]["text"] == "Export allows two loops per canvas."
    assert "narrowed" in edit["note"]
    assert edit["claims"][0]["quote"] == "export now supports two loops per canvas"
    reasons = " | ".join(d["reason"] for d in ch["dropped"])
    assert "a PR alone can't change" in reasons and "unverified or unknown claims" in reasons
    assert ch["notes"] == ["nothing else"]


async def test_docs_page_change_is_always_a_product_change(seeded, scripted):
    answers, _ = scripted
    change = {
        "slug": "evals",
        "title": "Evaluations",
        "hash": "abc123",
        "url": "https://calfus.dev.aetherion.io/docs/evals",
        "status": "changed",
        "added": ["Evals can now run on a schedule."],
        "removed": [],
    }
    key = events.save_event(events.docs_event(change, "2026-11-12"))
    answers["route"] = RouteResult(product_change=False)
    route = await _fn(understand.route_event_tool)(key)
    assert route["product_change"] and route["features"][0] == "evaluations"


def _queued_items():
    arch = catalog.load_architecture()
    ev = events.pr_event(PR)
    claims = {
        "c1": {
            "id": "c1",
            "statement": "s",
            "evidence_id": "e1",
            "quote": "export now supports two loops",
            "about": "canvas",
        }
    }
    make = lambda op, tier: proposals.make_proposal(  # noqa: E731
        ev,
        {"claim_ids": ["c1"], "target_type": "feature", **op},
        claims,
        tier,
        catalog.load_feature(op["target"]),
        arch,
    )
    return [
        make(
            {"op": "add_update", "target": "canvas", "kind": "changed", "summary": "Two loops."},
            "A",
        ),
        make({"op": "edit_field", "target": "canvas", "field": "summary", "text": "New."}, "B"),
        make({"op": "edit_field", "target": "aether", "field": "summary", "text": "Newer."}, "B"),
        make({"op": "edit_field", "target": "evaluations", "field": "summary", "text": "N."}, "B"),
    ]


async def test_queue_apply_review_and_report(seeded):
    items = _queued_items()
    shadow = await _fn(apply_tools.queue_proposals_tool)("run-0", items, False)
    assert shadow == {"auto": [], "review": [], "pending": 0}
    assert proposals.load_pending() == []

    q = await _fn(apply_tools.queue_proposals_tool)("run-1", items, True)
    assert q["auto"] == [items[0]["id"]] and len(q["review"]) == 3
    assert q["review"][0]["before"] == catalog.load_feature("canvas")["summary"]

    auto = await _fn(apply_tools.apply_proposals_tool)("run-1", q["auto"])
    assert [r["status"] for r in auto["results"]] == ["applied"]

    asked = [r["id"] for r in q["review"]]
    decided = {asked[0]: "approve", asked[1]: "reject"}
    out = await _fn(apply_tools.record_review_tool)("run-1", asked, decided, "wrong", "ann")
    assert (out["approved"], out["rejected"], out["unanswered"], out["expired"]) == (1, 1, 1, 0)
    assert catalog.load_feature("canvas")["summary"] == "New."
    assert [p["id"] for p in proposals.load_pending()] == [asked[2]]

    for run in ("run-2", "run-3"):  # unanswered three times -> expired
        out = await _fn(apply_tools.record_review_tool)(run, [asked[2]], {}, "", None)
    assert out["expired"] == 1 and proposals.load_pending() == []

    audit = storage.get_json(proposals.run_key("run-1"))
    assert {"proposed", "applied", "rejected"} <= set(audit)
    assert audit["rejected"][0]["decided_by"] == "ann"

    written = await _fn(apply_tools.write_report_tool)(
        "run-1",
        {
            "run_id": "run-1",
            "mode": "live",
            "started_on": "2026-11-12",
            "events": [],
            "proposals": [{**items[1], "status": "applied"}],
            "applied": auto["results"],
            "dropped": [],
            "gaps": [],
            "refresh": [],
            "review": {"asked": 3, "approved": 1},
        },
    )
    text = storage.get(written["file_key"]).decode()
    assert "## Applied" in text and "Update on canvas (changed)" in text
    assert "[B] Edit canvas: summary" in text
