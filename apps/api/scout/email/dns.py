"""MX/A lookups (dnspython, async) with a 30-day Postgres cache and per-domain catch-all memory.

The cache (`domain_dns_cache`, global) is best-effort: a database failure never breaks a lookup.
Transient resolver failures (timeouts, SERVFAIL) are returned but never cached.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import dns.asyncresolver
import dns.exception
import dns.resolver
import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scout.config import get_settings
from scout.db.engine import session_scope
from scout.db.models import DomainDnsCache
from scout.email.syntax import normalize_domain

log = structlog.get_logger(__name__)

CACHE_TTL = timedelta(days=30)
DNS_TIMEOUT_S = 3.0
NULL_MX_ERROR = "null_mx"  # RFC 7505: the domain explicitly accepts no mail


@dataclass
class MxInfo:
    domain: str
    has_mx: bool
    mx_hosts: list[str] = field(default_factory=list)  # by preference, best first
    has_a: bool = False
    error: str | None = None
    null_mx: bool = False
    transient: bool = False  # resolver failure: outcome unknown, not cached
    cached: bool = False

    @property
    def accepts_mail(self) -> bool:
        """MX hosts, or an implicit MX via A/AAAA (RFC 5321 §5.1) unless a null MX is published."""
        return not self.null_mx and (self.has_mx or self.has_a)


def make_resolver() -> dns.asyncresolver.Resolver:
    r = dns.asyncresolver.Resolver()
    r.timeout = DNS_TIMEOUT_S
    r.lifetime = DNS_TIMEOUT_S
    return r


# Indirection so tests (and alternative deployments) can inject a resolver.
resolver_factory: Callable[[], dns.asyncresolver.Resolver] = make_resolver


async def _has_address(resolver: dns.asyncresolver.Resolver, domain: str) -> bool:
    for rdtype in ("A", "AAAA"):
        try:
            answer = await resolver.resolve(domain, rdtype)
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except dns.exception.DNSException:
            return False
        if len(answer):
            return True
    return False


async def resolve_mx(domain: str) -> MxInfo:
    """Uncached MX (+ A/AAAA) resolution of an already normalized domain."""
    resolver = resolver_factory()
    try:
        answer = await resolver.resolve(domain, "MX")
    except dns.resolver.NXDOMAIN:
        return MxInfo(domain, has_mx=False, has_a=False, error="NXDOMAIN")
    except dns.resolver.NoAnswer:
        records: list[tuple[int, str]] = []
    except dns.exception.DNSException as exc:
        return MxInfo(domain, has_mx=False, error=f"dns_error:{type(exc).__name__}", transient=True)
    else:
        records = sorted((int(r.preference), r.exchange.to_text().rstrip(".").lower()) for r in answer)
    has_a = await _has_address(resolver, domain)
    hosts = [h for _, h in records if h]
    if records and not hosts:  # "0 ." only → null MX
        return MxInfo(domain, has_mx=False, has_a=has_a, error=NULL_MX_ERROR, null_mx=True)
    return MxInfo(domain, has_mx=bool(hosts), mx_hosts=list(dict.fromkeys(hosts)), has_a=has_a)


async def _cache_get(domain: str) -> MxInfo | None:
    try:
        async with session_scope() as s:
            row = await s.get(DomainDnsCache, domain)
    except Exception as exc:
        log.warning("email.dns.cache_read_failed", domain=domain, error=str(exc))
        return None
    if row is None or row.has_mx is None or row.checked_at < datetime.now(UTC) - CACHE_TTL:
        return None
    return MxInfo(
        domain,
        has_mx=bool(row.has_mx),
        mx_hosts=[str(h) for h in (row.mx_hosts or [])],
        has_a=bool(row.has_a),
        error=row.error,
        null_mx=bool(row.null_mx) or row.error == NULL_MX_ERROR,
        cached=True,
    )


async def _cache_put(info: MxInfo) -> None:
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
        log.warning("email.dns.cache_write_failed", domain=info.domain, error=str(exc))


@lru_cache(maxsize=8)
def _fixture_domains(path: str) -> dict[str, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    domains = ((data or {}).get("email") or {}).get("domains") or {}
    return {d: spec for name, spec in domains.items() if (d := normalize_domain(name))}


def _fixture_mx(domain: str) -> MxInfo | None:
    """Fixture verifier backend (tests / offline E2E only): MX facts come from the manifest, never DNS.

    Same rules as `FixtureVerifier`: a listed domain has MX unless ``"mx": false``; unknown domains have none.
    """
    s = get_settings()
    if s.verifier_backend != "fixture" or s.is_production or not s.discovery_fixture_manifest:
        return None
    spec = _fixture_domains(str(s.discovery_fixture_manifest)).get(domain)
    if spec is None or not spec.get("mx", True):
        return MxInfo(domain, has_mx=False, error="nxdomain")
    return MxInfo(domain, has_mx=True, mx_hosts=[f"mx.{domain}"], has_a=True)


async def mx_lookup(domain: str, *, use_cache: bool = True) -> MxInfo:
    """MX info for a domain; served from `domain_dns_cache` when checked within 30 days."""
    d = normalize_domain(domain)
    if d is None:
        return MxInfo(domain or "", has_mx=False, error="invalid_domain")
    if (fixture := _fixture_mx(d)) is not None:
        return fixture
    if use_cache and (cached := await _cache_get(d)) is not None:
        return cached
    info = await resolve_mx(d)
    if use_cache and not info.transient:
        await _cache_put(info)
    return info


async def cached_catch_all(domain: str) -> bool | None:
    """Catch-all verdict remembered for the domain (None when unknown or older than 30 days)."""
    d = normalize_domain(domain)
    if d is None:
        return None
    try:
        async with session_scope() as s:
            row = (
                await s.execute(
                    sa.select(DomainDnsCache.catch_all, DomainDnsCache.catch_all_checked_at).where(
                        DomainDnsCache.domain == d
                    )
                )
            ).first()
    except Exception as exc:
        log.warning("email.dns.catch_all_read_failed", domain=d, error=str(exc))
        return None
    if row is None or row.catch_all is None or row.catch_all_checked_at is None:
        return None
    if row.catch_all_checked_at < datetime.now(UTC) - CACHE_TTL:
        return None
    return bool(row.catch_all)


async def store_catch_all(domain: str, value: bool | None) -> None:
    """Remember a definitive catch-all verdict (None is ignored: keeps the previous verdict)."""
    d = normalize_domain(domain)
    if d is None or value is None:
        return
    values = {"catch_all": value, "catch_all_checked_at": sa.func.now()}
    stmt = pg_insert(DomainDnsCache).values(domain=d, **values)
    stmt = stmt.on_conflict_do_update(index_elements=[DomainDnsCache.domain], set_=values)
    try:
        async with session_scope() as s:
            await s.execute(stmt)
    except Exception as exc:
        log.warning("email.dns.catch_all_write_failed", domain=d, error=str(exc))
