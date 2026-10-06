"""Public GitHub commit evidence for a company domain (pattern evidence only).

Used only when the company's own website links to a GitHub organisation/user. We list its most
recently pushed public repositories (≤ 3, forks skipped) and read the latest commits of each; author
and committer addresses ON THE COMPANY DOMAIN with a plausible person name become
``ObservedEmail(source=github)``.

Ethics / compliance: public REST API data only, on-domain business addresses only, no profile
scraping. GitHub's Acceptable Use Policy forbids using commit emails for unsolicited email, so these
samples are *pattern evidence only*: they keep ``source=github`` and the engine never proposes them
as a contact address.

Budget: process-wide sliding window (60 requests/hour unauthenticated, 5,000 with ``GITHUB_TOKEN``),
plus the server's ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset``; a 403/429 rate-limit answer
blocks every further call until the reset time (or ``Retry-After``).
"""

from __future__ import annotations

import re
import time
from collections import Counter, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlsplit

import structlog

from scout.config import get_settings
from scout.db.enums import EmailEvidenceSource, UsageCategory
from scout.discovery.common import http_request
from scout.email.contracts import ObservedEmail
from scout.email.intel.state import parse_iso
from scout.email.lists import is_role_local_part
from scout.email.patterns import infer_pattern
from scout.email.syntax import normalize_address, split_address
from scout.errors import FetchError, PermanentError, RateLimitedError
from scout.extract.names import is_plausible_person_name, split_name
from scout.services.usage import record_usage
from scout.util.pools import pool

log = structlog.get_logger(__name__)

GITHUB_FRESHNESS = timedelta(days=30)
GITHUB_ERROR_BACKOFF = timedelta(days=1)
MAX_REPOS = 3
REPOS_PER_PAGE = 30
COMMITS_PER_PAGE = 100
UNAUTHENTICATED_PER_HOUR = 60
AUTHENTICATED_PER_HOUR = 5000
DEFAULT_BLOCK_S = 60.0
GITHUB_SAMPLE_CONFIDENCE = 0.85

_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_RESERVED = frozenset(
    """
    about account apps blog codespaces collections contact customer-stories enterprise enterprises events
    explore features github-copilot home issues join login logout marketplace new notifications organizations
    orgs pricing pulls readme search security sessions settings site sponsors stars team topics trending
    users solutions resources partners premium-support signup copilot
    """.split()
)
_BOT_LOCAL = re.compile(
    r"(^|[^a-z])(bot|bots|ci|cd|build|builder|deploy|deployer|automation|robot)([^a-z]|$)"
)
_BOT_NAME = re.compile(r"(^|[^a-z])(bot|bots|robot|automation)([^a-z]|$)")


class GitHubRateLimited(Exception):
    def __init__(self, retry_at: float) -> None:
        super().__init__(f"github rate limited until {retry_at:.0f}")
        self.retry_at = retry_at


@dataclass
class GitHubEvidence:
    login: str
    repos: list[str] = field(default_factory=list)
    emails: list[ObservedEmail] = field(default_factory=list)
    requests: int = 0
    rate_limited: bool = False
    not_found: bool = False
    error: str | None = None

    @property
    def completed(self) -> bool:
        """The check ran to the end (possibly finding nothing): safe to mark the domain as checked."""
        return not self.rate_limited and self.error is None


# ---- budget -------------------------------------------------------------------------------------


class GitHubBudget:
    """Process-wide request budget and rate-limit back-off for the GitHub REST API."""

    def __init__(self) -> None:
        self.calls: deque[float] = deque()
        self.blocked_until = 0.0
        self.remaining: int | None = None
        self.reset_at = 0.0

    def _limit(self, authenticated: bool) -> int:
        return AUTHENTICATED_PER_HOUR if authenticated else UNAUTHENTICATED_PER_HOUR

    def available(self, authenticated: bool, *, now: float | None = None) -> bool:
        try:
            self.check(authenticated, now=now)
        except GitHubRateLimited:
            return False
        return True

    def check(self, authenticated: bool, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        while self.calls and self.calls[0] <= now - 3600:
            self.calls.popleft()
        if now < self.blocked_until:
            raise GitHubRateLimited(self.blocked_until)
        if self.remaining is not None and self.remaining <= 0 and now < self.reset_at:
            raise GitHubRateLimited(self.reset_at)
        if len(self.calls) >= self._limit(authenticated):
            raise GitHubRateLimited(self.calls[0] + 3600)

    def spend(self, *, now: float | None = None) -> None:
        self.calls.append(time.time() if now is None else now)

    def observe(self, headers: Mapping[str, str]) -> None:
        remaining, reset = headers.get("x-ratelimit-remaining"), headers.get("x-ratelimit-reset")
        if remaining is not None and remaining.strip().isdigit():
            self.remaining = int(remaining)
        if reset is not None and reset.strip().isdigit():
            self.reset_at = float(reset)

    def block(self, until: float) -> None:
        self.blocked_until = max(self.blocked_until, until)


_budget = GitHubBudget()


def budget() -> GitHubBudget:
    return _budget


def reset_budget() -> None:
    """Tests."""
    global _budget
    _budget = GitHubBudget()


def github_token(token: str | None = None) -> str | None:
    """Explicit token, else ``GITHUB_TOKEN`` from settings; None when unauthenticated."""
    if token is not None:
        return token or None
    secret = get_settings().github_token
    value = secret.get_secret_value() if secret else ""
    return value or None


def _rate_limit_wait(status: int, headers: Mapping[str, str], body: str) -> float | None:
    """Epoch until which we must stop, when the answer is a rate-limit refusal."""
    if status not in (403, 429):
        return None
    limited = (
        status == 429
        or headers.get("x-ratelimit-remaining") == "0"
        or "rate limit" in body.lower()
        or "retry-after" in headers
    )
    if not limited:
        return None
    now = time.time()
    retry_after = headers.get("retry-after", "").strip()
    if retry_after.isdigit():
        return now + float(retry_after)
    reset = headers.get("x-ratelimit-reset", "").strip()
    if reset.isdigit():
        return max(now + 1.0, float(reset))
    return now + DEFAULT_BLOCK_S


async def _get(path: str, params: dict[str, Any], token: str | None) -> tuple[int, Any]:
    _budget.check(token is not None)
    _budget.spend()
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = get_settings().github_api_url.rstrip("/") + path
    async with pool("public_api"):
        resp = await http_request(
            "GET",
            url,
            source="github_intel",
            params=params,
            headers=headers,
            allow=(401, 403, 404, 409, 422, 429, 451),
        )
    await record_usage(UsageCategory.registry_request, source_key="github", resolver="domain_intel")
    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
    _budget.observe(resp_headers)
    until = _rate_limit_wait(resp.status_code, resp_headers, resp.text)
    if until is not None:
        _budget.block(until)
        log.warning("email.intel.github_rate_limited", until=until, path=path)
        raise GitHubRateLimited(until)
    if resp.status_code != 200:
        return resp.status_code, None
    try:
        return 200, resp.json()
    except ValueError:
        return 200, None


# ---- login discovery ----------------------------------------------------------------------------


def github_login_from_url(url: str | None) -> str | None:
    """`https://github.com/<login>[/…]` (or `/orgs/<login>`) → login; None for other URLs."""
    if not url or not isinstance(url, str):
        return None
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in ("github.com", "www.github.com"):
        return None
    segments = [seg for seg in parts.path.split("/") if seg]
    if not segments:
        return None
    login = segments[1] if segments[0].lower() == "orgs" and len(segments) > 1 else segments[0]
    if login.lower() in _RESERVED or not _LOGIN_RE.match(login):
        return None
    return login.lower()


def logins_from_links(links_list: Iterable[Mapping[str, Any] | None]) -> Counter[str]:
    """Count GitHub logins linked from cached pages (``website_pages.links`` as stored by the parser)."""
    counts: Counter[str] = Counter()
    for links in links_list:
        if not isinstance(links, Mapping):
            continue
        urls: list[Any] = []
        social = links.get("social")
        if isinstance(social, Mapping):
            urls.extend(social.values())
        for bucket in ("external", "internal"):
            for item in links.get(bucket) or []:
                urls.append(item.get("url") if isinstance(item, Mapping) else item)
        for url in urls:
            login = github_login_from_url(url) if isinstance(url, str) else None
            if login:
                counts[login] += 1
    return counts


# ---- commits → observed emails ------------------------------------------------------------------


def select_repos(repos: Iterable[Mapping[str, Any]], limit: int = MAX_REPOS) -> list[Mapping[str, Any]]:
    """Most recently pushed public, non-fork, non-empty repositories."""
    usable = [
        r
        for r in repos
        if isinstance(r, Mapping)
        and not r.get("fork")
        and not r.get("private")
        and not r.get("disabled")
        and r.get("full_name")
        and (r.get("size") or 0) > 0
    ]
    usable.sort(key=lambda r: str(r.get("pushed_at") or ""), reverse=True)
    return usable[:limit]


def _is_bot(name: str, local: str, account: Mapping[str, Any] | None) -> bool:
    if account and (
        str(account.get("type") or "").lower() == "bot" or str(account.get("login") or "").endswith("[bot]")
    ):
        return True
    low = name.lower()
    return "[bot]" in low or bool(_BOT_NAME.search(low)) or bool(_BOT_LOCAL.search(local))


def _person_name(raw: str) -> tuple[str, str] | None:
    name = " ".join(raw.split())
    if not name or "@" in name:
        return None
    if name.islower():
        name = name.title()
    if not is_plausible_person_name(name):
        return None
    first, last = split_name(name)
    return (first, last) if first and last else None


def commit_emails(
    commits: Iterable[Mapping[str, Any]], domain: str, *, repo: str | None = None
) -> list[ObservedEmail]:
    """Author/committer addresses on `domain` with a plausible person name (one entry per address,
    latest commit wins). Skips noreply addresses, bots, role mailboxes and non-person names."""
    found: dict[str, ObservedEmail] = {}
    for item in commits:
        if not isinstance(item, Mapping):
            continue
        commit = item.get("commit") or {}
        sha = str(item.get("sha") or "")
        for role in ("author", "committer"):
            person = commit.get(role) or {}
            addr = normalize_address(person.get("email"))
            if addr is None:
                continue
            local, adom = split_address(addr)
            if not (adom == domain or adom.endswith("." + domain)):
                continue
            if "noreply" in local or "no-reply" in local or is_role_local_part(local):
                continue
            raw_name = str(person.get("name") or "")
            if _is_bot(raw_name, local, item.get(role)):
                continue
            names = _person_name(raw_name)
            if names is None:
                continue
            first, last = names
            when = parse_iso(person.get("date"))
            obs = ObservedEmail(
                address=addr,
                local_part=local,
                source=EmailEvidenceSource.github,
                first_name=first,
                last_name=last,
                pattern=infer_pattern(first, last, local),
                is_role=False,
                source_url=item.get("html_url") or None,
                evidence=f"commit {sha[:7]}" + (f" in {repo}" if repo else "") + f" ({role}: {first} {last})",
                confidence=GITHUB_SAMPLE_CONFIDENCE,
                observed_at=when,
            )
            prev = found.get(addr)
            if prev is None or _better(obs, prev):
                found[addr] = obs
    return list(found.values())


def _better(new: ObservedEmail, old: ObservedEmail) -> bool:
    """Prefer a name that renders the address, then the most recent commit."""
    if (new.pattern is None) != (old.pattern is None):
        return new.pattern is not None
    return new.observed_at is not None and (old.observed_at is None or new.observed_at > old.observed_at)


async def fetch_github_evidence(
    login: str, domain: str, *, token: str | None = None, max_repos: int = MAX_REPOS
) -> GitHubEvidence:
    """List `login`'s recently pushed repos and collect on-domain commit addresses (≤ 1 + max_repos calls)."""
    ev = GitHubEvidence(login=login)
    tok = github_token(token)
    safe_login = quote(login, safe="")
    try:
        status, repos = await _get(
            f"/users/{safe_login}/repos",
            {"sort": "pushed", "direction": "desc", "per_page": REPOS_PER_PAGE, "type": "owner"},
            tok,
        )
        ev.requests += 1
        if status == 404:
            ev.not_found = True
            return ev
        if status != 200 or not isinstance(repos, list):
            ev.error = f"repos_http_{status}"
            return ev
        found: dict[str, ObservedEmail] = {}
        for repo in select_repos(repos, max_repos):
            full_name = str(repo["full_name"])
            owner, _, name = full_name.partition("/")
            status, commits = await _get(
                f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/commits",
                {"per_page": COMMITS_PER_PAGE},
                tok,
            )
            ev.requests += 1
            ev.repos.append(full_name)
            if status != 200 or not isinstance(commits, list):
                continue  # 409: empty repository
            for obs in commit_emails(commits, domain, repo=full_name):
                prev = found.get(obs.address)
                if prev is None or _better(obs, prev):
                    found[obs.address] = obs
        ev.emails = list(found.values())
    except GitHubRateLimited:
        ev.rate_limited = True
    except (FetchError, PermanentError, RateLimitedError) as exc:
        ev.error = str(exc)[:200]
    log.info(
        "email.intel.github",
        login=login,
        domain=domain,
        repos=len(ev.repos),
        emails=len(ev.emails),
        requests=ev.requests,
        rate_limited=ev.rate_limited,
        error=ev.error,
    )
    return ev
