import pytest

from utils.confluence_url import extract_confluence_url

CLOUD = "https://calfus.atlassian.net/wiki/spaces/ENG/pages/123456/Export+v2"


@pytest.fixture(autouse=True)
def _hosts(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://calfus.atlassian.net/wiki")
    monkeypatch.setenv("CONFLUENCE_ALLOWED_HOSTS", "wiki.calfus.com")


@pytest.mark.parametrize(
    "body",
    [
        f"Feature documentation: {CLOUD}",
        f"**Feature documentation:** {CLOUD}.",
        f"- Feature documentation: [Export v2 spec]({CLOUD})",
        f"feature documentation - <{CLOUD}>",
        f"## Feature documentation\n\n{CLOUD}\n\n## Testing",
        f"Summary\r\n\r\nFeature Documentation: {CLOUD}\r\n",
    ],
)
def test_labeled_cloud_link(body):
    r = extract_confluence_url(body)
    assert r["status"] == "ok"
    assert r["url"] == CLOUD
    assert (r["page_id"], r["space_key"]) == ("123456", "ENG")


@pytest.mark.parametrize(
    ("url", "ref"),
    [
        ("https://calfus.atlassian.net/wiki/spaces/ENG/pages/42", {"page_id": "42"}),
        ("https://calfus.atlassian.net/wiki/x/AbCd_1", {"tiny_id": "AbCd_1"}),
        ("https://wiki.calfus.com/pages/viewpage.action?pageId=99", {"page_id": "99"}),
        (
            "https://wiki.calfus.com/confluence/display/ENG/Export+Spec",
            {"space_key": "ENG", "page_title": "Export Spec"},
        ),
    ],
)
def test_supported_formats(url, ref):
    r = extract_confluence_url(f"Feature documentation: {url}")
    assert r["status"] == "ok", r["reason"]
    assert {k: r[k] for k in ref} == ref


@pytest.mark.parametrize(
    "body",
    [
        "",
        "No docs for this one.",
        f"See the spec at {CLOUD}",  # unlabeled links are not guessed
        "Feature documentation: TBD",
        f"<!-- Feature documentation: {CLOUD} -->",  # template placeholder
        "## Feature documentation\n\n## Testing\n",
    ],
)
def test_missing(body):
    assert extract_confluence_url(body)["status"] == "missing"


def test_ambiguous():
    body = (
        f"Feature documentation: {CLOUD}\n"
        "Feature documentation: https://calfus.atlassian.net/wiki/spaces/ENG/pages/7"
    )
    assert extract_confluence_url(body)["status"] == "ambiguous"


def test_same_link_twice_is_not_ambiguous():
    body = f"Feature documentation: {CLOUD}\n\nFeature documentation: [spec]({CLOUD})"
    assert extract_confluence_url(body)["status"] == "ok"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.atlassian.net/wiki/spaces/ENG/pages/1",
        "https://calfus.atlassian.net.evil.com/wiki/spaces/ENG/pages/1",
        "https://calfus.atlassian.net@evil.com/wiki/spaces/ENG/pages/1",
        "https://docs.google.com/document/d/abc",
    ],
)
def test_host_not_approved(url):
    assert extract_confluence_url(f"Feature documentation: {url}")["status"] == "host_not_approved"


@pytest.mark.parametrize(
    "url",
    [
        "http://calfus.atlassian.net/wiki/spaces/ENG/pages/1",
        "https://calfus.atlassian.net:8443/wiki/spaces/ENG/pages/1",
        "https://calfus.atlassian.net/wiki/spaces/ENG/overview",
        "https://calfus.atlassian.net/wiki/pages/viewpage.action?pageId=abc",
        "https://calfus.atlassian.net/browse/PROJ-1",
    ],
)
def test_unsupported_format(url):
    assert extract_confluence_url(f"Feature documentation: {url}")["status"] == (
        "unsupported_format"
    )


def test_no_hosts_configured(monkeypatch):
    monkeypatch.delenv("CONFLUENCE_BASE_URL")
    monkeypatch.delenv("CONFLUENCE_ALLOWED_HOSTS")
    assert extract_confluence_url(f"Feature documentation: {CLOUD}")["status"] == (
        "host_not_approved"
    )
