"""GitHub organisations adapter (search + org details, rate limits)."""

from __future__ import annotations

import httpx
import pytest
import respx

from scout.discovery.base import DiscoveryQuery
from scout.discovery.common import Throttle
from scout.discovery.github import GitHubSource
from scout.errors import RateLimitedError

from .conftest import defn

GH = "https://api.github.com"

SEARCH = {
    "total_count": 2,
    "incomplete_results": False,
    "items": [
        {"login": "acme-saas", "id": 1, "type": "Organization", "url": f"{GH}/users/acme-saas"},
        {"login": "hobby-org", "id": 2, "type": "Organization", "url": f"{GH}/users/hobby-org"},
        {"login": "some-user", "id": 3, "type": "User", "url": f"{GH}/users/some-user"},
    ],
}
ACME = {
    "login": "acme-saas",
    "name": "Acme SaaS",
    "blog": "https://www.acme-saas.com",
    "location": "Austin, TX",
    "email": "Hello@acme-saas.com",
    "description": "B2B billing APIs",
    "public_repos": 42,
    "followers": 120,
    "html_url": "https://github.com/acme-saas",
    "created_at": "2019-04-02T10:00:00Z",
}
HOBBY = {"login": "hobby-org", "name": "Hobby", "blog": "https://hobby-org.github.io", "location": "Austin"}


@pytest.fixture
def src() -> GitHubSource:
    return GitHubSource(throttle=Throttle(0))


def query() -> DiscoveryQuery:
    return DiscoveryQuery(key="gh", params={"q": 'saas type:org location:"Austin"'})


@respx.mock
async def test_orgs_with_company_websites(src: GitHubSource, settings_env) -> None:
    settings_env(GITHUB_TOKEN="ghp_test")
    search = respx.get(f"{GH}/search/users").mock(return_value=httpx.Response(200, json=SEARCH))
    respx.get(f"{GH}/orgs/acme-saas").mock(return_value=httpx.Response(200, json=ACME))
    respx.get(f"{GH}/orgs/hobby-org").mock(return_value=httpx.Response(200, json=HOBBY))
    page = await src.discover(query(), None)
    assert [c.name for c in page.candidates] == ["Acme SaaS"]  # github.io blog is not a company site
    c = page.candidates[0]
    assert (
        c.website == "https://acme-saas.com/"
        and c.domain == "acme-saas.com"
        and c.emails == ["hello@acme-saas.com"]
    )
    assert (
        c.location == {"city": "Austin", "country": "US"} and c.source_url == "https://github.com/acme-saas"
    )
    req = search.calls.last.request
    assert req.url.params["q"] == 'saas type:org location:"Austin"'
    assert req.headers["authorization"] == "Bearer ghp_test"
    assert page.next_cursor is None and page.requests == 3


@respx.mock
async def test_rate_limit(src: GitHubSource) -> None:
    respx.get(f"{GH}/search/users").mock(
        return_value=httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
        )
    )
    with pytest.raises(RateLimitedError):
        await src.discover(query(), None)


def test_plan_and_suitability(settings_env) -> None:
    src = GitHubSource()
    d = defn(industries=["SaaS startups"], countries=["US"])
    assert src.suitability(d) == 0.4
    settings_env(GITHUB_TOKEN="ghp_x")
    assert src.suitability(d) == 0.6
    plan = src.plan(d)
    assert plan[0].params["q"] == 'saas type:org location:"New York"'
    assert src.suitability(defn(industries=["dentist"], countries=["US"])) == 0
