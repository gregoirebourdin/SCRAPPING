"""GitHub commit evidence: login discovery, commit filtering, budget and rate-limit back-off (respx, no network)."""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from scout.config import get_settings
from scout.db.enums import EmailEvidenceSource
from scout.email.intel import github as gh

DOMAIN = "acme.fr"


@pytest.fixture(autouse=True)
def _fresh_budget():
    gh.reset_budget()
    yield
    gh.reset_budget()


def api() -> str:
    return get_settings().github_api_url.rstrip("/")


def commit(sha, name, email, date="2026-05-01T10:00:00Z", *, committer=None, account=None):
    c_name, c_email = committer or (name, email)
    return {
        "sha": sha,
        "html_url": f"https://github.com/acme/app/commit/{sha}",
        "commit": {
            "author": {"name": name, "email": email, "date": date},
            "committer": {"name": c_name, "email": c_email, "date": date},
        },
        "author": account,
        "committer": None,
    }


# ---- login discovery ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "login"),
    [
        ("https://github.com/AcmeCorp", "acmecorp"),
        ("https://www.github.com/acme-corp/backend", "acme-corp"),
        ("github.com/orgs/acme/people", "acme"),
        ("http://github.com/acme/", "acme"),
        ("https://github.com/features/actions", None),
        ("https://github.com/sponsors/someone", None),
        ("https://github.com/", None),
        ("https://gitlab.com/acme", None),
        ("https://acme.github.io/", None),
        ("https://github.com/-bad-", None),
        ("not a url", None),
        (None, None),
    ],
)
def test_github_login_from_url(url, login):
    assert gh.github_login_from_url(url) == login


def test_logins_from_parser_links():
    pages = [
        {
            "social": {"linkedin": "https://linkedin.com/company/acme"},
            "external": [{"url": "https://github.com/acme", "text": "GitHub"}],
            "internal": [],
        },
        {
            "social": {"github": "https://github.com/acme"},
            "external": [{"url": "https://github.com/gohugoio"}],
        },
        None,
        {"external": ["https://github.com/acme/app"]},
    ]
    counts = gh.logins_from_links(pages)
    assert counts.most_common(1) == [("acme", 3)] and counts["gohugoio"] == 1


# ---- commit filtering -------------------------------------------------------------------------


def test_commit_emails_keeps_on_domain_people_only():
    commits = [
        commit("a1", "Marie Dupont", "marie.dupont@acme.fr", "2026-05-01T10:00:00Z"),
        commit("a2", "Marie Dupont", "Marie.Dupont@ACME.fr", "2026-06-01T10:00:00Z"),  # later: wins
        commit("b1", "jean martin", "jean.martin@paris.acme.fr"),  # lowercase name, subdomain
        commit("c1", "Paul Durand", "pdurand@gmail.com"),  # free provider / off-domain
        commit("d1", "Léa Petit", "12345+lpetit@users.noreply.github.com"),  # noreply
        commit("e1", "dependabot[bot]", "bot@acme.fr", account={"login": "dependabot[bot]", "type": "Bot"}),
        commit("e2", "Deploy Bot", "deploy-bot@acme.fr"),
        commit("e3", "Hugo Bernard", "ci-bot@acme.fr"),
        commit("f1", "Acme Support", "support@acme.fr"),  # role mailbox
        commit("g1", "jdupont", "jdupont@acme.fr"),  # login, not a person name
        commit("h1", "Chloé Thomas", "chloe.thomas@acme.fr", committer=("GitHub", "noreply@github.com")),
        commit("i1", "Louis Robert", "lr.dev@acme.fr"),  # person, but the name does not render it
        {"sha": "x", "commit": None},
        "garbage",
    ]
    found = {o.address: o for o in gh.commit_emails(commits, DOMAIN, repo="acme/app")}
    assert set(found) == {
        "marie.dupont@acme.fr",
        "jean.martin@paris.acme.fr",
        "chloe.thomas@acme.fr",
        "lr.dev@acme.fr",
    }
    marie = found["marie.dupont@acme.fr"]
    assert marie.source == EmailEvidenceSource.github and marie.pattern == "{first}.{last}"
    assert marie.source_url == "https://github.com/acme/app/commit/a2" and "a2" in (marie.evidence or "")
    assert marie.observed_at is not None and marie.observed_at.month == 6
    assert (found["jean.martin@paris.acme.fr"].first_name, found["jean.martin@paris.acme.fr"].last_name) == (
        "Jean",
        "Martin",
    )
    assert found["lr.dev@acme.fr"].pattern is None  # kept as an observation, teaches no pattern


def test_select_repos_skips_forks_and_empty_repos():
    repos = [
        {"full_name": "acme/old", "pushed_at": "2024-01-01T00:00:00Z", "size": 10},
        {"full_name": "acme/fork", "pushed_at": "2026-09-01T00:00:00Z", "size": 10, "fork": True},
        {"full_name": "acme/empty", "pushed_at": "2026-09-02T00:00:00Z", "size": 0},
        {"full_name": "acme/api", "pushed_at": "2026-08-01T00:00:00Z", "size": 5},
        {"full_name": "acme/web", "pushed_at": "2026-07-01T00:00:00Z", "size": 5},
        {"full_name": "acme/docs", "pushed_at": "2026-06-01T00:00:00Z", "size": 5},
    ]
    assert [r["full_name"] for r in gh.select_repos(repos)] == ["acme/api", "acme/web", "acme/docs"]


# ---- API calls --------------------------------------------------------------------------------


REPOS = [
    {"full_name": "acme/api", "pushed_at": "2026-08-01T00:00:00Z", "size": 5},
    {"full_name": "acme/fork", "pushed_at": "2026-09-01T00:00:00Z", "size": 5, "fork": True},
]


@respx.mock
async def test_fetch_github_evidence_authenticated():
    repos = respx.get(f"{api()}/users/acme/repos").mock(return_value=httpx.Response(200, json=REPOS))
    commits = respx.get(f"{api()}/repos/acme/api/commits").mock(
        return_value=httpx.Response(
            200,
            json=[
                commit("a1", "Marie Dupont", "marie.dupont@acme.fr"),
                commit("b1", "Jean Martin", "jm@other.io"),
            ],
            headers={"x-ratelimit-remaining": "4999", "x-ratelimit-reset": str(int(time.time()) + 3600)},
        )
    )
    ev = await gh.fetch_github_evidence("acme", DOMAIN, token="ghp_test")
    assert ev.completed and not ev.rate_limited and ev.requests == 2 and ev.repos == ["acme/api"]
    assert [o.address for o in ev.emails] == ["marie.dupont@acme.fr"]
    assert repos.calls.last.request.headers["authorization"] == "Bearer ghp_test"
    assert repos.calls.last.request.url.params["sort"] == "pushed"
    assert commits.calls.last.request.url.params["per_page"] == "100"
    assert gh.budget().remaining == 4999


@respx.mock
async def test_fetch_github_evidence_unauthenticated_and_not_found():
    route = respx.get(f"{api()}/users/ghost/repos").mock(return_value=httpx.Response(404, json={}))
    ev = await gh.fetch_github_evidence("ghost", DOMAIN, token="")
    assert ev.not_found and ev.completed and ev.emails == [] and ev.requests == 1
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_rate_limit_403_blocks_until_reset():
    reset = int(time.time()) + 900
    route = respx.get(f"{api()}/users/acme/repos").mock(
        return_value=httpx.Response(
            403,
            json={"message": "API rate limit exceeded for 1.2.3.4."},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
        )
    )
    ev = await gh.fetch_github_evidence("acme", DOMAIN, token="")
    assert ev.rate_limited and not ev.completed and route.call_count == 1
    assert gh.budget().blocked_until == pytest.approx(reset)
    assert not gh.budget().available(False)
    again = await gh.fetch_github_evidence("acme", DOMAIN, token="")
    assert again.rate_limited and route.call_count == 1  # no request while blocked


@respx.mock
async def test_secondary_rate_limit_429_honours_retry_after():
    respx.get(f"{api()}/users/acme/repos").mock(
        return_value=httpx.Response(429, headers={"retry-after": "120"})
    )
    before = time.time()
    ev = await gh.fetch_github_evidence("acme", DOMAIN, token="")
    assert ev.rate_limited
    assert before + 119 <= gh.budget().blocked_until <= time.time() + 121


@respx.mock
async def test_exhausted_remaining_header_stops_further_calls():
    respx.get(f"{api()}/users/acme/repos").mock(
        return_value=httpx.Response(
            200,
            json=REPOS,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 600)},
        )
    )
    commits = respx.get(f"{api()}/repos/acme/api/commits").mock(return_value=httpx.Response(200, json=[]))
    ev = await gh.fetch_github_evidence("acme", DOMAIN, token="")
    assert ev.rate_limited and commits.call_count == 0


@respx.mock
async def test_non_rate_limit_403_and_server_errors_are_errors():
    respx.get(f"{api()}/users/acme/repos").mock(
        return_value=httpx.Response(
            403, json={"message": "Resource protected by organization SAML enforcement."}
        )
    )
    ev = await gh.fetch_github_evidence("acme", DOMAIN, token="")
    assert ev.error == "repos_http_403" and not ev.rate_limited and gh.budget().available(False)
    respx.get(f"{api()}/users/broken/repos").mock(return_value=httpx.Response(502))
    ev = await gh.fetch_github_evidence("broken", DOMAIN, token="")
    assert ev.error and not ev.completed


def test_budget_sliding_window():
    b = gh.GitHubBudget()
    now = 1_000_000.0
    for i in range(gh.UNAUTHENTICATED_PER_HOUR):
        b.check(False, now=now + i)
        b.spend(now=now + i)
    with pytest.raises(gh.GitHubRateLimited) as exc:
        b.check(False, now=now + 100)
    assert exc.value.retry_at == now + 3600
    b.check(True, now=now + 100)  # the token budget is larger
    b.check(False, now=now + 3601)  # the oldest call left the window
