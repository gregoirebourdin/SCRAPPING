"""Domain Intelligence Profile: computed once per domain, reused by every contact, campaign and workspace.

``get_domain_intel`` assembles MX + provider, website-published addresses (named from the company's
people records), public GitHub commit evidence and RDAP contacts when stale and relevant, the learned
patterns and the SMTP facts written by the SMTP layer (``update_smtp_facts``).

Caching, from cheapest to most durable:

1. in-process TTL cache (10 min) keyed by the request shape, behind a per-domain ``asyncio.Lock`` —
   20 concurrent contacts of one domain trigger ONE build;
2. the ``domain_profiles`` row, with one freshness window per component: MX 7 days, website on input
   change (pages / people fingerprint) or 7 days, GitHub 30 days, RDAP 90 days, patterns 30 days;
3. cross-process leases (``stats.claims``) so two workers never call GitHub/RDAP for the same domain.

GitHub / RDAP are network evidence: skipped under ``APP_ENV=test`` unless a test enables them
(``state.set_network_evidence(True)``), and skipped while backing off after an error (1 day) or while
the GitHub budget is exhausted (the component is then simply not refreshed).

Change flags (``MX_CHANGED``, ``PATTERN_CHANGED``, ``CATCH_ALL_FLIPPED``) are recorded in
``stats.flags`` + ``evidence`` and bump ``stats.invalidated_at`` (verdicts older than it should be
re-verified). An MX change also clears the SMTP facts, which belonged to the previous servers.
"""

from __future__ import annotations

import re
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scout.db.engine import session_scope
from scout.db.enums import EmailEvidenceSource, MailProvider, SmtpHealthState, SmtpResult
from scout.db.models import (
    Company,
    CompanyFieldObservation,
    DomainDnsCache,
    DomainProfile,
    Person,
    WebsitePage,
)
from scout.email import dns as edns
from scout.email.contracts import DomainIntel, DomainProbeResult, ObservedEmail, PatternStat, SessionOutcome
from scout.email.intel import github, learning, rdap, samples, state
from scout.email.intel.providers import detect_provider, provider_notes
from scout.email.lists import is_disposable_domain, is_free_provider, is_role_local_part
from scout.email.patterns import infer_pattern
from scout.email.syntax import normalize_address, normalize_domain, split_address

log = structlog.get_logger(__name__)

MX_FRESHNESS = timedelta(days=7)
WEBSITE_FRESHNESS = timedelta(days=7)
CLAIM_LEASE = timedelta(minutes=5)
WEBSITE_SAMPLE_CONFIDENCE = 0.9
MAX_OBSERVED_EVIDENCE = 30


# ---- helpers ------------------------------------------------------------------------------------


def _fresh(at: datetime | None, window: timedelta, now: datetime) -> bool:
    return at is not None and now - at < window


def _ev(
    signal: str,
    value: Any,
    source: str,
    *,
    source_url: str | None = None,
    observed_at: datetime | str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    at = state.iso(observed_at) if isinstance(observed_at, datetime) else observed_at
    return {
        "signal": signal,
        "value": value,
        "source": source,
        "source_url": source_url,
        "observed_at": at,
        **extra,
    }


def person_names(first: str | None, last: str | None, full_name: str | None) -> tuple[str | None, str | None]:
    """(first, last) of a people record (same rule as the finder: full_name split when needed)."""
    if first and last:
        return first, last
    tokens = (full_name or "").split()
    if len(tokens) >= 2:
        return first or tokens[0], last or " ".join(tokens[1:])
    return first or (tokens[0] if tokens else None), last


def _uses_both_names(pattern: str) -> bool:
    return ("{first}" in pattern or "{f}" in pattern) and ("{last}" in pattern or "{l}" in pattern)


def match_person(local: str, people: Sequence[tuple[str | None, str | None]]) -> tuple[str, str] | None:
    """The one person whose name renders `local` (``{f}{l}`` excluded); None when nobody or ambiguous.

    With several candidates, those matched through both a first and a last name win ("marie.dupont"
    beats a bare "marie"); remaining ties are ambiguous.
    """
    matches: dict[tuple[str, str], str] = {}
    for first, last in people:
        if not (first and last):
            continue
        pattern = infer_pattern(first, last, local)
        if pattern is None or pattern in learning.WEAK_PATTERNS:
            continue
        matches.setdefault((first, last), pattern)
    if len(matches) > 1:
        matches = {k: p for k, p in matches.items() if _uses_both_names(p)}
    return next(iter(matches)) if len(matches) == 1 else None


def _page_addresses(emails: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    for item in emails or []:
        raw = item if isinstance(item, str) else None
        if isinstance(item, Mapping):
            raw = item.get("address") or item.get("email") or item.get("value")
        addr = normalize_address(raw) if isinstance(raw, str) else None
        if addr:
            out.append(addr)
    return out


def website_items(
    domain: str,
    pages: Sequence[tuple[str, Iterable[Any] | None, datetime | None]],
    people: Sequence[tuple[str | None, str | None]],
) -> list[ObservedEmail]:
    """On-domain addresses published on cached pages (as extracted by the crawler; no text rewriting),
    named only from the company's people records."""
    seen: set[str] = set()
    out: list[ObservedEmail] = []
    for url, emails, fetched_at in pages:
        for addr in _page_addresses(emails):
            local, adom = split_address(addr)
            if addr in seen or not samples.is_on_domain(adom, domain):
                continue
            seen.add(addr)
            role = is_role_local_part(local)
            names = None if role else match_person(local, people)
            out.append(
                ObservedEmail(
                    address=addr,
                    local_part=local,
                    source=EmailEvidenceSource.website,
                    first_name=names[0] if names else None,
                    last_name=names[1] if names else None,
                    is_role=role,
                    source_url=url,
                    evidence="published on the company website",
                    confidence=WEBSITE_SAMPLE_CONFIDENCE,
                    observed_at=fetched_at,
                )
            )
    return out


@dataclass
class _WorkspaceContext:
    items: list[ObservedEmail] = field(default_factory=list)
    logins: Counter[str] = field(default_factory=Counter)
    fingerprint: str = "none"
    company_size_max: int | None = None
    country: str | None = None


async def _workspace_context(
    domain: str, workspace_id: uuid.UUID, company_id: uuid.UUID | None
) -> _WorkspaceContext:
    """Workspace-scoped reads: the company's cached pages, people and social observations."""
    async with session_scope() as s:
        q = sa.select(Company.id, Company.employee_max, Company.country).where(
            Company.workspace_id == workspace_id
        )
        if company_id is not None:
            q = q.where(Company.id == company_id)
        else:
            q = q.where(sa.or_(Company.normalized_domain == domain, Company.domain == domain))
        companies = (await s.execute(q)).all()
        ids = [c.id for c in companies]
        if not ids:
            return _WorkspaceContext()
        pages = (
            await s.execute(
                sa.select(WebsitePage.url, WebsitePage.emails, WebsitePage.links, WebsitePage.fetched_at)
                .where(WebsitePage.workspace_id == workspace_id, WebsitePage.company_id.in_(ids))
                .order_by(WebsitePage.fetched_at.desc())
            )
        ).all()
        people = (
            await s.execute(
                sa.select(Person.first_name, Person.last_name, Person.full_name, Person.updated_at).where(
                    Person.workspace_id == workspace_id, Person.company_id.in_(ids)
                )
            )
        ).all()
        github_obs = (
            await s.scalars(
                sa.select(CompanyFieldObservation.value_json).where(
                    CompanyFieldObservation.workspace_id == workspace_id,
                    CompanyFieldObservation.company_id.in_(ids),
                    CompanyFieldObservation.field_name == "social_github",
                )
            )
        ).all()
    names = [person_names(p.first_name, p.last_name, p.full_name) for p in people]
    logins = github.logins_from_links(p.links for p in pages)
    for value in github_obs:
        login = github.github_login_from_url(value if isinstance(value, str) else None)
        if login:
            logins[login] += 1
    last_page = max((p.fetched_at for p in pages if p.fetched_at), default=None)
    last_person = max((p.updated_at for p in people if p.updated_at), default=None)
    fp = f"{len(pages)}:{state.iso(last_page)}:{len(people)}:{state.iso(last_person)}"
    return _WorkspaceContext(
        items=website_items(domain, [(p.url, p.emails, p.fetched_at) for p in pages], names),
        logins=logins,
        fingerprint=fp,
        company_size_max=max((c.employee_max for c in companies if c.employee_max), default=None),
        country=next((c.country for c in companies if c.country), None),
    )


def _provider_for(info: edns.MxInfo, domain: str) -> MailProvider:
    if info.null_mx or not info.mx_hosts:
        return MailProvider.none
    return detect_provider(info.mx_hosts, domain=domain)


async def _store_dns(info: edns.MxInfo) -> None:
    """Refresh the shared ``domain_dns_cache`` row after a live lookup (best effort)."""
    values = {
        "has_mx": info.has_mx,
        "mx_hosts": info.mx_hosts,
        "has_a": info.has_a,
        "null_mx": info.null_mx,
        "error": info.error,
        "checked_at": sa.func.now(),
    }
    stmt = pg_insert(DomainDnsCache).values(domain=info.domain, **values)
    stmt = stmt.on_conflict_do_update(index_elements=[DomainDnsCache.domain], set_=values)
    try:
        async with session_scope() as s:
            await s.execute(stmt)
    except Exception as exc:
        log.warning("email.intel.dns_cache_write_failed", domain=info.domain, error=str(exc))


async def _lookup_mx(
    domain: str, *, refresh: bool, dns_checked_at: datetime | None, now: datetime
) -> edns.MxInfo:
    """MX info no older than MX_FRESHNESS: the shared DNS cache when recent enough, else a live lookup."""
    if not refresh and _fresh(dns_checked_at, MX_FRESHNESS, now):
        return await edns.mx_lookup(domain)
    info = await edns.mx_lookup(domain, use_cache=False)
    if not info.transient and info.error != "invalid_domain":
        await _store_dns(info)
    return info


async def _claim(domain: str, component: str, now: datetime) -> bool:
    """Cross-process lease on an expensive component (GitHub, RDAP) of one domain."""
    async with session_scope() as s:
        row = await s.get(DomainProfile, domain, with_for_update=True, populate_existing=True)
        if row is None:
            return False
        stats = dict(row.stats or {})
        claims = dict(stats.get("claims") or {})
        held = state.parse_iso(claims.get(component))
        if held is not None and now - held < CLAIM_LEASE:
            return False
        claims[component] = state.iso(now)
        stats["claims"] = claims
        row.stats = stats
    return True


def _reset_smtp_facts(row: DomainProfile) -> None:
    row.catch_all = None
    row.catch_all_confidence = None
    row.catch_all_method = None
    row.catch_all_checked_at = None
    row.smtp_reachable = None
    row.smtp_last_result = None
    row.smtp_checked_at = None
    row.greylisting_seen = False


def _evidence(
    row: DomainProfile,
    stats: Mapping[str, Any],
    patterns: Sequence[PatternStat],
    observed: Sequence[ObservedEmail],
) -> list[dict[str, Any]]:
    provider = MailProvider(row.provider)
    out = [
        _ev("mx_hosts", list(row.mx_hosts or []), "dns", observed_at=row.mx_checked_at, has_mx=row.has_mx),
        _ev(
            "provider",
            provider.value,
            "mx",
            observed_at=row.mx_checked_at,
            notes=provider_notes(provider, row.mx_hosts or []),
        ),
        _ev("accepts_mail", row.accepts_mail, "dns", observed_at=row.mx_checked_at),
    ]
    if row.catch_all is not None:
        out.append(
            _ev(
                "catch_all",
                row.catch_all,
                "smtp",
                observed_at=row.catch_all_checked_at,
                confidence=row.catch_all_confidence,
                method=row.catch_all_method,
            )
        )
    if row.smtp_reachable is not None or row.smtp_last_result:
        out.append(
            _ev(
                "smtp",
                row.smtp_last_result,
                "smtp",
                observed_at=row.smtp_checked_at,
                reachable=row.smtp_reachable,
                greylisting_seen=row.greylisting_seen,
            )
        )
    for p in patterns[:3]:
        out.append(
            _ev(
                "pattern",
                p.pattern,
                "learning",
                observed_at=p.last_confirmed_at,
                confidence=p.confidence,
                share=p.share,
                samples=p.samples,
                successes=p.successes,
                failures=p.failures,
            )
        )
    if patterns:
        out.append(
            _ev("dominant_pattern_lower_bound", learning.dominant_lower_bound(list(patterns)), "learning")
        )
    for o in observed[:MAX_OBSERVED_EVIDENCE]:
        out.append(
            _ev(
                "observed_email",
                o.address,
                o.source.value,
                source_url=o.source_url,
                observed_at=o.observed_at,
                named=bool(o.first_name and o.last_name),
                is_role=o.is_role,
                pattern=o.pattern,
                pattern_evidence_only=o.source == EmailEvidenceSource.github,
            )
        )
    if row.github_org:
        gh = stats.get("github") or {}
        out.append(
            _ev(
                "github_org",
                row.github_org,
                "github",
                source_url=f"https://github.com/{row.github_org}",
                observed_at=row.github_checked_at,
                repos=gh.get("repos"),
                emails=gh.get("emails"),
            )
        )
    rd = stats.get("rdap") or {}
    if rd.get("registrant_org"):
        out.append(
            _ev(
                "rdap_registrant_org",
                rd["registrant_org"],
                "rdap",
                source_url=rd.get("source_url"),
                observed_at=row.rdap_checked_at,
            )
        )
    if rd.get("registrar"):
        out.append(
            _ev(
                "rdap_registrar",
                rd["registrar"],
                "rdap",
                source_url=rd.get("source_url"),
                observed_at=row.rdap_checked_at,
            )
        )
    out.extend(state.flag_evidence(stats))
    if stats.get("invalidated_at"):
        out.append(
            _ev(
                "invalidated_at", stats["invalidated_at"], "domain_intel", observed_at=stats["invalidated_at"]
            )
        )
    return out


def _intel_from_row(
    row: DomainProfile,
    stats: Mapping[str, Any],
    patterns: list[PatternStat],
    observed: list[ObservedEmail],
    hits: dict[str, bool],
    now: datetime,
) -> DomainIntel:
    return DomainIntel(
        domain=row.domain,
        provider=MailProvider(row.provider),
        mx_hosts=[str(h) for h in (row.mx_hosts or [])],
        has_mx=row.has_mx,
        accepts_mail=row.accepts_mail,
        catch_all=row.catch_all,
        catch_all_confidence=row.catch_all_confidence,
        smtp_reachable=row.smtp_reachable,
        greylisting_seen=bool(row.greylisting_seen),
        patterns=patterns,
        observed=observed,
        evidence=_evidence(row, stats, patterns, observed),
        cache_hits=hits,
        built_at=now,
    )


def _bump(stats: dict[str, Any], key: str, n: int = 1) -> None:
    stats[key] = int(stats.get(key) or 0) + n


# ---- build --------------------------------------------------------------------------------------


async def _minimal_intel(domain: str) -> DomainIntel:
    """Free-provider / disposable domains: MX facts only, no samples, no learning, no profile row."""
    info = await edns.mx_lookup(domain)
    hits = {c: False for c in state.COMPONENTS} | {"mx": info.cached, "profile": False}
    kind = "free_provider" if is_free_provider(domain) else "disposable"
    provider = _provider_for(info, domain)
    return DomainIntel(
        domain=domain,
        provider=provider,
        mx_hosts=list(info.mx_hosts),
        has_mx=None if info.transient else info.has_mx,
        accepts_mail=None if info.transient else info.accepts_mail,
        evidence=[
            _ev(kind, True, "lists"),
            _ev("mx_hosts", list(info.mx_hosts), "dns", has_mx=info.has_mx, error=info.error),
            _ev("provider", provider.value, "mx", notes=provider_notes(provider, info.mx_hosts)),
        ],
        cache_hits=hits,
        built_at=state.utcnow(),
    )


async def _build(
    d: str,
    *,
    workspace_id: uuid.UUID | None,
    company_id: uuid.UUID | None,
    refresh: bool,
    include_github: bool,
    include_rdap: bool,
    company_size_max: int | None,
    country: str | None,
) -> DomainIntel:
    started = time.monotonic()
    now = state.utcnow()
    hits = {c: False for c in state.COMPONENTS} | {"profile": False}
    refreshed: list[str] = []
    samples_changed = False

    async with session_scope() as s:
        await learning.ensure_profile(s, d)
        prev = await s.get(DomainProfile, d, populate_existing=True)
        dns_checked_at = await s.scalar(
            sa.select(DomainDnsCache.checked_at).where(
                DomainDnsCache.domain == d, DomainDnsCache.has_mx.is_not(None)
            )
        )
    assert prev is not None
    prev_stats: dict[str, Any] = dict(prev.stats or {})

    # 1. MX + provider
    mx: edns.MxInfo | None = None
    if not refresh and prev.has_mx is not None and _fresh(prev.mx_checked_at, MX_FRESHNESS, now):
        hits["mx"] = True
    else:
        mx = await _lookup_mx(d, refresh=refresh, dns_checked_at=dns_checked_at, now=now)
        hits["mx"] = mx.cached
        if mx.transient:
            log.warning("email.intel.mx_transient", domain=d, error=mx.error)
        else:
            refreshed.append("mx")

    # 2. Website (workspace-scoped pages and people)
    ctx = _WorkspaceContext()
    website_fp: str | None = None
    if workspace_id is not None:
        ctx = await _workspace_context(d, workspace_id, company_id)
        checked = (prev_stats.get("website_checked") or {}).get(str(workspace_id)) or {}
        if (
            not refresh
            and checked.get("fp") == ctx.fingerprint
            and _fresh(state.parse_iso(checked.get("at")), WEBSITE_FRESHNESS, now)
        ):
            hits["website"] = True
        else:
            if ctx.items:
                n = await samples.record_observed_emails(
                    d, ctx.items, workspace_id=workspace_id, relearn=False
                )
                samples_changed = samples_changed or n > 0
            website_fp = ctx.fingerprint
            refreshed.append("website")
    else:
        hits["website"] = prev.website_checked_at is not None
    size = company_size_max if company_size_max is not None else ctx.company_size_max
    cc = (country or ctx.country or "").upper() or None

    # 3. GitHub (only when the company's own site links to a GitHub account)
    network = state.network_evidence_enabled()
    gh: github.GitHubEvidence | None = None
    if include_github and network:
        login = ctx.logins.most_common(1)[0][0] if ctx.logins else prev.github_org
        stale = (
            refresh
            or not _fresh(prev.github_checked_at, github.GITHUB_FRESHNESS, now)
            or (login is not None and prev.github_org is not None and login != prev.github_org)
        )
        backoff = not refresh and _fresh(
            state.parse_iso(prev_stats.get("github_error_at")), github.GITHUB_ERROR_BACKOFF, now
        )
        authenticated = github.github_token() is not None
        if not stale:
            hits["github"] = True
        elif (
            login
            and not backoff
            and github.budget().available(authenticated)
            and await _claim(d, "github", now)
        ):
            gh = await github.fetch_github_evidence(login, d)
            if gh.emails:
                n = await samples.record_observed_emails(d, gh.emails, relearn=False)
                samples_changed = samples_changed or n > 0
            if gh.completed:
                refreshed.append("github")

    # 4. RDAP
    rd: rdap.RdapResult | None = None
    if include_rdap and network:
        stale = refresh or not _fresh(prev.rdap_checked_at, rdap.RDAP_FRESHNESS, now)
        backoff = not refresh and _fresh(
            state.parse_iso(prev_stats.get("rdap_error_at")), rdap.RDAP_ERROR_BACKOFF, now
        )
        if not stale:
            hits["rdap"] = True
        elif not backoff and await _claim(d, "rdap", now):
            rd = await rdap.fetch_rdap(d)
            if rd.emails:
                n = await samples.record_observed_emails(d, rd.emails, relearn=False)
                samples_changed = samples_changed or n > 0
            if rd.completed:
                refreshed.append("rdap")

    # 5. Patterns
    ctx_prior = prev_stats.get("prior_context") or {}
    context_changed = (size is not None or cc is not None) and (
        ctx_prior.get("company_size_max") != size or ctx_prior.get("country") != cc
    )
    if samples_changed or refresh or context_changed or not prev_stats.get("patterns_v2"):
        patterns = await learning.relearn_domain_patterns(d, company_size_max=size, country=cc)
        refreshed.append("patterns")
    else:
        patterns = await learning.load_pattern_stats(d)
        hits["patterns"] = True

    # 6. Observed addresses (visibility: public sources + this workspace's private ones)
    observed = samples.unique_by_address(await samples.load_observed(d, workspace_id=workspace_id))

    # 7. Write the components this build owns, assemble from the locked row
    async with session_scope() as s:
        row = await s.get(DomainProfile, d, with_for_update=True, populate_existing=True)
        assert row is not None
        stats: dict[str, Any] = dict(row.stats or {})
        if mx is not None and not mx.transient:
            provider = _provider_for(mx, d)
            hosts = list(mx.mx_hosts)
            if row.mx_checked_at is not None and (
                sorted(row.mx_hosts or []) != sorted(hosts) or MailProvider(row.provider) != provider
            ):
                stats = state.add_flag(
                    stats,
                    state.MX_CHANGED,
                    {
                        "from": {"provider": str(row.provider), "mx_hosts": list(row.mx_hosts or [])},
                        "to": {"provider": provider.value, "mx_hosts": hosts},
                    },
                    now,
                )
                _reset_smtp_facts(row)
                log.info("email.intel.mx_changed", domain=d, provider=provider.value)
            row.provider = provider
            row.mx_hosts = hosts
            row.has_mx = mx.has_mx
            row.accepts_mail = mx.accepts_mail
            row.mx_checked_at = now
        if website_fp is not None and workspace_id is not None:
            stats["website_checked"] = {
                **(stats.get("website_checked") or {}),
                str(workspace_id): {"at": state.iso(now), "fp": website_fp},
            }
            row.website_checked_at = now
        claims = dict(stats.get("claims") or {})
        if gh is not None:
            claims.pop("github", None)
            if gh.completed:
                row.github_checked_at = now
                row.github_org = gh.login
                stats.pop("github_error_at", None)
                stats["github"] = {
                    "login": gh.login,
                    "repos": gh.repos,
                    "emails": len(gh.emails),
                    "requests": gh.requests,
                    "not_found": gh.not_found,
                }
            elif gh.error:
                stats["github_error_at"] = state.iso(now)
        if rd is not None:
            claims.pop("rdap", None)
            if rd.completed:
                row.rdap_checked_at = now
                stats.pop("rdap_error_at", None)
                stats["rdap"] = {
                    "status": rd.status,
                    "registrant_org": rd.registrant_org,
                    "registrar": rd.registrar,
                    "source_url": rd.source_url,
                    "emails": len(rd.emails),
                }
            elif rd.status == "error":
                stats["rdap_error_at"] = state.iso(now)
        stats["claims"] = claims
        _bump(stats, "builds")
        _bump(stats, "cache_hits", state.take_hits(d))
        refreshes = dict(stats.get("refreshes") or {})
        for component in refreshed:
            refreshes[component] = int(refreshes.get(component) or 0) + 1
        stats["refreshes"] = refreshes
        stats["last_build_ms"] = int((time.monotonic() - started) * 1000)
        row.built_at = now
        intel = _intel_from_row(row, stats, patterns, observed, hits, now)
        row.evidence = intel.evidence
        row.stats = stats
    log.info("email.intel.build", domain=d, refreshed=refreshed, hits=[k for k, v in hits.items() if v])
    return intel


# ---- public API ---------------------------------------------------------------------------------


async def get_domain_intel(
    domain: str,
    *,
    workspace_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    refresh: bool = False,
    include_github: bool = True,
    include_rdap: bool = True,
    company_size_max: int | None = None,
    country: str | None = None,
) -> DomainIntel:
    """Domain Intelligence Profile for `domain` (built once, reused; see module doc for freshness).

    `workspace_id` / `company_id` scope the website read (cached pages + people names) and the
    visibility of private samples; the profile itself is global.
    """
    d = normalize_domain(domain)
    if d is None:
        return DomainIntel(
            domain=(domain or "").strip().lower(),
            evidence=[_ev("invalid_domain", domain, "input")],
            built_at=state.utcnow(),
        )
    key = (
        d,
        workspace_id,
        company_id,
        include_github,
        include_rdap,
        company_size_max,
        (country or "").upper() or None,
    )
    if not refresh and (cached := state.cache_get(key)) is not None:
        state.count_hit(d)
        return _from_memory(cached)
    async with state.lock_for(d):
        if not refresh and (cached := state.cache_get(key)) is not None:
            state.count_hit(d)
            return _from_memory(cached)
        if is_free_provider(d) or is_disposable_domain(d):
            intel = await _minimal_intel(d)
        else:
            intel = await _build(
                d,
                workspace_id=workspace_id,
                company_id=company_id,
                refresh=refresh,
                include_github=include_github,
                include_rdap=include_rdap,
                company_size_max=company_size_max,
                country=country,
            )
        state.cache_put(key, intel)
        return state.snapshot(intel)


def _from_memory(intel: DomainIntel) -> DomainIntel:
    """A cached profile (already a private copy) flagged as fully served from cache."""
    intel.cache_hits = {c: True for c in state.COMPONENTS} | {"profile": True}
    return intel


_GREYLIST_FALLBACK = re.compile(r"gr[ae]y-?list", re.IGNORECASE)


def _probe_greylisted(probe: DomainProbeResult) -> bool:
    flag = getattr(probe, "greylisted", None)
    if isinstance(flag, bool):
        return flag
    try:
        from scout.email.smtp.classify import is_greylisting
    except ImportError:  # pragma: no cover - SMTP layer always present in production

        def is_greylisting(message: str) -> bool:
            return bool(_GREYLIST_FALLBACK.search(message or ""))

    return any(
        v.result == SmtpResult.temporary and is_greylisting(v.message) for v in probe.verdicts.values()
    )


def facts_from_probe(probe: DomainProbeResult, smtp_state: SmtpHealthState | None = None) -> dict[str, Any]:
    """`update_smtp_facts` keyword arguments derived from one probe session (empty when not attempted).

    Reachability is only denied when the session failed at the infrastructure level while our own SMTP
    path is HEALTHY (others answer us: this domain's MX is the problem).
    """
    if probe.session == SessionOutcome.not_attempted:
        return {}
    facts: dict[str, Any] = {"smtp_last_result": probe.session.value}
    if probe.session in (SessionOutcome.ok, SessionOutcome.temporary):
        facts["smtp_reachable"] = True
    elif probe.session == SessionOutcome.infra_failure and smtp_state == SmtpHealthState.HEALTHY:
        facts["smtp_reachable"] = False
    if _probe_greylisted(probe):
        facts["greylisting_seen"] = True
    if probe.catch_all is not None:
        facts["catch_all"] = probe.catch_all
        facts["catch_all_confidence"] = probe.catch_all_confidence
        facts["catch_all_method"] = f"smtp_random_probes:{probe.verifier}"
    return facts


async def update_smtp_facts(
    domain: str,
    probe: DomainProbeResult | None = None,
    *,
    catch_all: bool | None = None,
    catch_all_confidence: float | None = None,
    catch_all_method: str | None = None,
    smtp_reachable: bool | None = None,
    smtp_last_result: str | None = None,
    greylisting_seen: bool | None = None,
    smtp_state: SmtpHealthState | None = None,
) -> None:
    """SMTP layer → profile: store what was learned about the domain's servers (None = unchanged).

    Facts come from keyword arguments and/or a probe session (`probe`, see :func:`facts_from_probe`);
    explicit keywords win. A catch-all verdict that flips records ``CATCH_ALL_FLIPPED`` and bumps
    ``stats.invalidated_at``.
    """
    d = normalize_domain(domain)
    if d is None:
        return
    if probe is not None:
        derived = facts_from_probe(probe, smtp_state)
        catch_all = catch_all if catch_all is not None else derived.get("catch_all")
        catch_all_confidence = (
            catch_all_confidence if catch_all_confidence is not None else derived.get("catch_all_confidence")
        )
        catch_all_method = catch_all_method or derived.get("catch_all_method")
        smtp_reachable = smtp_reachable if smtp_reachable is not None else derived.get("smtp_reachable")
        smtp_last_result = smtp_last_result or derived.get("smtp_last_result")
        greylisting_seen = (
            greylisting_seen if greylisting_seen is not None else derived.get("greylisting_seen")
        )
    now = state.utcnow()
    async with session_scope() as s:
        await learning.ensure_profile(s, d)
        row = await s.get(DomainProfile, d, with_for_update=True, populate_existing=True)
        assert row is not None
        stats = dict(row.stats or {})
        if catch_all is not None:
            if row.catch_all is not None and row.catch_all != catch_all:
                stats = state.add_flag(
                    stats,
                    state.CATCH_ALL_FLIPPED,
                    {"from": row.catch_all, "to": catch_all, "method": catch_all_method},
                    now,
                )
                log.info("email.intel.catch_all_flipped", domain=d, catch_all=catch_all)
            row.catch_all = catch_all
            row.catch_all_checked_at = now
        if catch_all_confidence is not None:
            row.catch_all_confidence = float(catch_all_confidence)
        if catch_all_method is not None:
            row.catch_all_method = catch_all_method
        smtp_touched = False
        if smtp_reachable is not None:
            row.smtp_reachable = smtp_reachable
            smtp_touched = True
        if smtp_last_result is not None:
            row.smtp_last_result = smtp_last_result
            smtp_touched = True
        if greylisting_seen is not None:
            row.greylisting_seen = greylisting_seen
            smtp_touched = True
        if smtp_touched:
            row.smtp_checked_at = now
        row.stats = stats
    state.invalidate(d)


async def profile_invalidated_at(domain: str) -> datetime | None:
    """When the domain's facts last changed materially (MX / dominant pattern / catch-all flip)."""
    d = normalize_domain(domain)
    if d is None:
        return None
    async with session_scope() as s:
        stats = await s.scalar(sa.select(DomainProfile.stats).where(DomainProfile.domain == d))
    return state.parse_iso((stats or {}).get("invalidated_at"))


async def flush_stats() -> int:
    """Write pending in-memory cache-hit counters into ``domain_profiles.stats``; returns domains updated."""
    pending = state.take_all_hits()
    updated = 0
    for d, n in pending.items():
        if n <= 0:
            continue
        async with session_scope() as s:
            row = await s.get(DomainProfile, d, with_for_update=True, populate_existing=True)
            if row is None:
                continue
            stats = dict(row.stats or {})
            _bump(stats, "cache_hits", n)
            row.stats = stats
            updated += 1
    return updated
