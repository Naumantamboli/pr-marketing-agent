"""ProductCatalogUpdater orchestration with scripted tools and a fake human reviewer."""

import logging
from types import SimpleNamespace

import pytest

from agent import agents


def _ev(n, kind="pr_merged"):
    return {
        "event_key": f"k{n}",
        "event_id": f"pr:org/app#{n}",
        "kind": kind,
        "title": f"PR {n}",
        "url": f"https://gh/{n}",
        "occurred_at": "2026-11-10",
        "notes": [],
    }


def _proposal(pid, tier):
    return {
        "id": pid,
        "tier": tier,
        "event_title": "PR 1",
        "event_url": "https://gh/1",
        "op": {
            "op": "add_update" if tier == "A" else "edit_field",
            "target": "canvas",
            "field": "summary",
            "kind": "changed",
            "summary": "x",
            "text": "y",
        },
        "preview": {"before": "old", "after": "new"},
        "claims": [{"quote": "q", "evidence_id": "e1", "source": "pr"}],
        "note": "",
    }


class Tools:
    """Scripted toolExecutor: records calls in order; handlers by tool name."""

    def __init__(self, **overrides):
        self.calls = []
        self.handlers = {
            "list_merged_prs_tool": lambda repos, days: {
                "prs": [{"url": "https://gh/1"}, {"url": "https://gh/2"}],
                "excluded": [{"url": "https://gh/3"}],
                "markers": {"org/app": {"m": 1}},
            },
            "load_retry_tool": lambda: [],
            "fetch_pr_event_tool": self._fetch,
            "collect_docs_events_tool": lambda day, shadow: {
                "events": [_ev(9, "docs_changed")],
                "baseline": False,
                "pages": 33,
            },
            "route_event_tool": lambda key: {
                "product_change": key != "k9",
                "features": ["canvas"],
                "components": [],
                "reason": "internal",
            },
            "extract_claims_tool": lambda key, route: {
                "claims": [{"id": "c1"}],
                "dropped": [],
                "gaps": ["g"],
            },
            "verify_claims_tool": lambda key, claims: {"claims": claims, "dropped": []},
            "compose_ops_tool": lambda key, route, claims: {"ops": [{"op": "add_update"}]},
            "check_ops_tool": lambda key, ops, claims: {
                "proposals": [_proposal("pA", "A"), _proposal("pB", "B")],
                "dropped": [],
                "notes": [],
            },
            "queue_proposals_tool": lambda run, items, live: {
                "auto": ["pA"] if live else [],
                "review": [
                    {
                        "id": "pB",
                        "tier": "B",
                        "headline": "Edit canvas: summary",
                        "event_title": "PR 1",
                        "event_url": "https://gh/1",
                        "before": "old",
                        "after": "new",
                        "evidence": [{"quote": "q", "source": "pr"}],
                        "note": "",
                    }
                ]
                if live
                else [],
                "pending": 1,
            },
            "apply_proposals_tool": lambda run, ids: {
                "results": [
                    {"id": i, "status": "applied", "headline": "h", "after": "a", "tier": "A"}
                    for i in ids
                ],
                "refresh": [],
                "overview_refresh": False,
            },
            "save_markers_tool": lambda markers: None,
            "promote_docs_snapshot_tool": lambda shadow: True,
            "save_retry_tool": lambda entries: None,
            "record_review_tool": lambda run, asked, decisions, notes, reviewer: {
                "results": [
                    {"id": i, "status": "applied", "headline": "h", "after": "a", "tier": "B"}
                    for i in asked
                    if decisions.get(i) == "approve"
                ],
                "refresh": ["canvas"],
                "overview_refresh": False,
                "approved": sum(d == "approve" for d in decisions.values()),
                "rejected": 0,
                "unanswered": 0,
                "expired": 0,
                "pending": 0,
            },
            "pending_count_tool": lambda: 0,
            "write_report_tool": lambda run, data: {"file_key": f"knowledge/runs/{run}.md"},
            **overrides,
        }

    @staticmethod
    def _fetch(url):
        if url.endswith("/2"):
            raise ValueError("GitHub returned 404")
        return _ev(1)

    async def execute(self, name, *args, **options):
        assert "start_to_close_timeout" in options, f"{name} called without a timeout"
        self.calls.append((name, args))
        result = self.handlers[name](*args)
        if isinstance(result, Exception):
            raise result
        return result

    def names(self):
        return [n for n, _ in self.calls]

    def args(self, name):
        return next(a for n, a in self.calls if n == name)


class Reviewer:
    def __init__(self, answers=None, timeout=False):
        self.answers, self.timeout, self.asked = answers or {}, timeout, None

    async def request(self, prompt, **kw):
        self.asked = (prompt, kw)
        if self.timeout:
            return None
        return SimpleNamespace(
            value=lambda n, default=None: self.answers.get(n, default), actor_id="ann"
        )


@pytest.fixture
def run(monkeypatch):
    def setup(tools, reviewer=None):
        monkeypatch.setattr(agents.toolExecutor, "execute", tools.execute)
        monkeypatch.setattr(agents.humanInput, "request", (reviewer or Reviewer()).request)
        monkeypatch.setattr(agents, "_run_id", lambda: "run-1")
        monkeypatch.setattr(agents, "_today", lambda: "2026-11-12")
        monkeypatch.setattr(agents, "workflow", SimpleNamespace(logger=logging.getLogger("t")))
        return agents.run_update

    return setup


async def test_live_run(run):
    tools, reviewer = Tools(), Reviewer({"p1": "approve"})
    result = await run(tools, reviewer)(
        {"repos": "org/app", "mode": "live", "reviewers": "teams:lead@x.com, owner@x.com"}
    )
    names = tools.names()
    # Markers, docs snapshot and retry list move on before the run waits for a person.
    assert names.index("save_markers_tool") < names.index("record_review_tool")
    assert names.index("promote_docs_snapshot_tool") < names.index("record_review_tool")
    assert tools.args("promote_docs_snapshot_tool") == (False,)
    assert tools.args("apply_proposals_tool") == ("run-1", ["pA"])
    assert tools.args("record_review_tool")[2] == {"pB": "approve"}

    prompt, kw = reviewer.asked
    assert "Edit canvas: summary" in prompt and '> "q" (pr)' in prompt
    assert [f.name for f in kw["fields"]] == ["p1", "notes"]
    assert kw["notify"] == [
        {"channel": "teams", "target": "lead@x.com"},
        {"channel": "slack", "target": "owner@x.com"},
    ]

    [retry] = tools.args("save_retry_tool")
    assert retry == [
        {"kind": "pr", "url": "https://gh/2", "error": "GitHub returned 404", "attempts": 1}
    ]
    report = tools.args("write_report_tool")[1]
    statuses = {e["event_key"]: e["status"] for e in report["events"]}
    assert statuses == {"k1": "proposed", "k9": "skipped"}
    assert {p["id"]: p["status"] for p in report["proposals"]} == {"pA": "applied", "pB": "applied"}
    assert report["excluded_prs"] == 1 and report["gaps"][0]["gap"] == "g"
    assert result["results"][0]["type"] == "s3_download_link"
    assert "2 applied" in result["message"] and "1 failed" in result["message"]


async def test_shadow_run_writes_nothing(run):
    tools = Tools()
    result = await run(tools)({"repos": "org/app"})
    names = tools.names()
    for forbidden in (
        "save_markers_tool",
        "apply_proposals_tool",
        "record_review_tool",
        "save_retry_tool",
        "load_retry_tool",
    ):
        assert forbidden not in names
    assert tools.args("queue_proposals_tool")[2] is False
    assert tools.args("promote_docs_snapshot_tool") == (True,)  # shadow snapshot only
    report = tools.args("write_report_tool")[1]
    assert {p["status"] for p in report["proposals"]} == {"not applied (shadow)"}
    assert result["message"].startswith("Shadow run")


async def test_one_failing_event_does_not_stop_the_others(run):
    def route(key):
        if key == "k1":
            raise RuntimeError("gateway down")
        return {"product_change": True, "features": ["canvas"], "components": []}

    tools = Tools(
        route_event_tool=route,
        load_retry_tool=lambda: [
            {"kind": "event", "event": _ev(7), "event_key": "k7", "attempts": 2, "error": "x"}
        ],
    )
    await run(tools, Reviewer(timeout=True))({"repos": "org/app", "mode": "live"})
    report = tools.args("write_report_tool")[1]
    statuses = {e["event_key"]: e["status"] for e in report["events"]}
    assert statuses["k1"] == "failed" and statuses["k9"] == "proposed"
    assert statuses["k7"] == "proposed"  # retried from last run, and it worked this time
    [retry] = tools.args("save_retry_tool")
    assert {r.get("url") or r["event_key"] for r in retry} == {"https://gh/2", "k1"}
    # Review timed out: nothing decided, items stay pending.
    assert tools.args("record_review_tool")[1:3] == (["pB"], {})


async def test_docs_failure_skips_snapshot_but_processes_prs(run):
    def broken(day, shadow):
        raise RuntimeError("docs site down")

    tools = Tools(collect_docs_events_tool=broken)
    await run(tools)({"repos": "org/app", "mode": "live"})
    assert "promote_docs_snapshot_tool" not in tools.names()
    assert tools.args("write_report_tool")[1]["docs"].startswith("not checked")


def test_settings():
    cfg = agents.settings(
        {
            "repos": "a/b,\n c/d",
            "lookback_days": "500",
            "include_docs": "false",
            "review_hours": 0,
            "mode": "LIVE",
        }
    )
    assert cfg["repos"] == ["a/b", "c/d"] and cfg["lookback_days"] == 90
    assert cfg["live"] and not cfg["include_docs"] and cfg["review_hours"] == 1
    with pytest.raises(ValueError):
        agents.settings({"mode": "yolo"})
    assert agents.notify_targets("sms:+14155550100, x@y.com") == [
        {"channel": "sms", "target": "+14155550100"},
        {"channel": "slack", "target": "x@y.com"},
    ]
