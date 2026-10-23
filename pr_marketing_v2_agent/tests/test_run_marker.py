from datetime import UTC, datetime

from utils.run_marker import initial_marker, select_batch

START = initial_marker(datetime(2026, 10, 1, tzinfo=UTC))


def pr(number, merged_at, decision="include"):
    return {
        "number": number,
        "merged_at": merged_at,
        "type_check": {"decision": decision},
    }


def run(found, markers, limit):
    return select_batch(found, markers, limit)


def test_takes_oldest_first_across_repos_and_marks_each_repo():
    found = {
        "a/web": [pr(1, "2026-10-02T00:00:00Z"), pr(2, "2026-10-04T00:00:00Z")],
        "a/api": [pr(7, "2026-10-03T00:00:00Z")],
    }
    r = run(found, {"a/web": START, "a/api": START}, limit=10)
    assert [(p["repo"], p["number"]) for p in r["prs"]] == [
        ("a/web", 1),
        ("a/api", 7),
        ("a/web", 2),
    ]
    assert not r["truncated"]
    assert r["markers"]["a/web"] == {"merged_at": "2026-10-04T00:00:00Z", "numbers": [2]}
    assert r["markers"]["a/api"] == {"merged_at": "2026-10-03T00:00:00Z", "numbers": [7]}


def test_excluded_prs_do_not_use_the_limit_but_advance_the_marker():
    found = {
        "a/web": [
            pr(1, "2026-10-02T00:00:00Z", "exclude"),
            pr(2, "2026-10-03T00:00:00Z", "exclude"),
            pr(3, "2026-10-04T00:00:00Z"),
            pr(4, "2026-10-05T00:00:00Z", "exclude"),
        ]
    }
    r = run(found, {"a/web": START}, limit=1)
    assert [p["number"] for p in r["prs"]] == [3]
    assert [p["number"] for p in r["excluded"]] == [1, 2, 4]
    assert not r["truncated"]
    assert r["markers"]["a/web"]["merged_at"] == "2026-10-05T00:00:00Z"


def test_unknown_type_counts_as_eligible():
    r = run({"a/web": [pr(1, "2026-10-02T00:00:00Z", "unknown")]}, {"a/web": START}, limit=5)
    assert [p["number"] for p in r["prs"]] == [1]


def test_truncation_leaves_the_rest_and_marker_stops_at_last_taken():
    found = {
        "a/web": [pr(n, f"2026-10-0{n + 1}T00:00:00Z") for n in (1, 2, 3)],
        "a/api": [pr(9, "2026-10-09T00:00:00Z")],
    }
    r = run(found, {"a/web": START, "a/api": START}, limit=2)
    assert [p["number"] for p in r["prs"]] == [1, 2]
    assert r["truncated"]
    assert r["markers"]["a/web"] == {"merged_at": "2026-10-03T00:00:00Z", "numbers": [2]}
    # Nothing from a/api was reached, so its marker doesn't move.
    assert r["markers"]["a/api"] == START


def test_ties_cut_by_the_limit_are_picked_up_next_run_without_repeats():
    same = "2026-10-05T12:00:00Z"
    found = {"a/web": [pr(10, same), pr(11, same), pr(12, same)]}

    first = run(found, {"a/web": START}, limit=2)
    assert [p["number"] for p in first["prs"]] == [10, 11]
    assert first["markers"]["a/web"] == {"merged_at": same, "numbers": [10, 11]}

    # list_merged_prs is inclusive, so the next run sees all three again.
    second = run(found, first["markers"], limit=2)
    assert [p["number"] for p in second["prs"]] == [12]
    assert not second["truncated"]
    assert second["markers"]["a/web"] == {"merged_at": same, "numbers": [10, 11, 12]}

    third = run(found, second["markers"], limit=2)
    assert third["prs"] == [] and third["excluded"] == []


def test_prs_before_the_marker_are_ignored():
    marker = {"merged_at": "2026-10-05T00:00:00Z", "numbers": [5]}
    found = {"a/web": [pr(4, "2026-10-04T00:00:00Z"), pr(5, "2026-10-05T00:00:00Z")]}
    r = run(found, {"a/web": marker}, limit=5)
    assert r["prs"] == []
    assert r["markers"]["a/web"] == marker


def test_no_prs_keeps_first_run_marker_so_the_window_is_anchored():
    r = run({"a/web": []}, {"a/web": START}, limit=5)
    assert r["markers"]["a/web"] == START


def test_does_not_mutate_input_markers():
    marker = {"merged_at": "2026-10-05T00:00:00Z", "numbers": [5]}
    run({"a/web": [pr(6, "2026-10-05T00:00:00Z")]}, {"a/web": marker}, limit=5)
    assert marker == {"merged_at": "2026-10-05T00:00:00Z", "numbers": [5]}
