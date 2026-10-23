from datetime import UTC, datetime

import httpx

from utils.github_client import GitHubClient

SINCE = datetime(2026, 10, 5, tzinfo=UTC)


def _pr(number, merged_at, updated_at, title="feat: thing", base="main"):
    return {
        "number": number,
        "html_url": f"https://github.com/a/web/pull/{number}",
        "title": title,
        "body": "",
        "labels": [],
        "head": {"ref": "feat/x"},
        "base": {"ref": base},
        "merged_at": merged_at,
        "updated_at": updated_at,
    }


def _client(pages):
    """GitHubClient whose /pulls pages come from `pages` (list of lists)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/a/web":
            return httpx.Response(200, json={"default_branch": "main"})
        page = int(request.url.params["page"])
        calls.append(page)
        return httpx.Response(200, json=pages[page - 1] if page <= len(pages) else [])

    gh = GitHubClient(token="t", api_url="https://api.github.test")
    gh._http = httpx.AsyncClient(
        base_url="https://api.github.test", transport=httpx.MockTransport(handler)
    )
    return gh, calls


async def test_includes_prs_merged_exactly_at_since_and_sorts_oldest_first():
    gh, _ = _client(
        [
            [
                _pr(3, "2026-10-06T00:00:00Z", "2026-10-06T00:00:00Z"),
                _pr(2, "2026-10-05T00:00:00Z", "2026-10-05T00:00:00Z"),  # == since
                _pr(1, "2026-10-04T00:00:00Z", "2026-10-07T00:00:00Z"),  # merged before
                _pr(4, None, "2026-10-07T00:00:00Z"),  # closed, not merged
            ]
        ]
    )
    async with gh:
        prs = await gh.list_merged_prs("a/web", SINCE, limit=10)
    assert [p["number"] for p in prs] == [2, 3]
    assert prs[0]["type_check"]["decision"] == "include"


async def test_keeps_paging_while_any_pr_is_updated_at_or_after_since():
    full_page = [_pr(100 + i, None, "2026-10-05T00:00:00Z") for i in range(100)]
    older_page = [_pr(200 + i, None, "2026-10-01T00:00:00Z") for i in range(100)]
    gh, calls = _client([full_page, older_page, [_pr(999, "2026-10-09T00:00:00Z", "x")]])
    async with gh:
        await gh.list_merged_prs("a/web", SINCE, limit=10)
    # Page 1 is updated exactly at `since`, so page 2 is read; page 2 is all older, so stop.
    assert calls == [1, 2]


async def test_limit_keeps_the_oldest():
    page = [_pr(n, f"2026-10-{10 + n}T00:00:00Z", f"2026-10-{10 + n}T00:00:00Z") for n in (3, 1, 2)]
    gh, _ = _client([page])
    async with gh:
        prs = await gh.list_merged_prs("a/web", SINCE, limit=2)
    assert [p["number"] for p in prs] == [1, 2]
