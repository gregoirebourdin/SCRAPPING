"""MX resolution with a mocked dnspython resolver (no real DNS, no database)."""

from __future__ import annotations

from types import SimpleNamespace

import dns.exception
import dns.name
import dns.resolver
import pytest

from scout.email import dns as edns


class FakeResolver:
    def __init__(self, answers: dict[tuple[str, str], object]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, qname: str, rdtype: str):
        self.calls.append((qname, rdtype))
        ans = self.answers.get((qname, rdtype), dns.resolver.NoAnswer())
        if isinstance(ans, BaseException):
            raise ans
        return ans


def mx(*records: tuple[int, str]):
    return [SimpleNamespace(preference=p, exchange=dns.name.from_text(h)) for p, h in records]


@pytest.fixture
def resolver(monkeypatch):
    holder: dict[str, FakeResolver] = {}

    def install(answers):
        holder["r"] = FakeResolver(answers)
        monkeypatch.setattr(edns, "resolver_factory", lambda: holder["r"])
        return holder["r"]

    return install


async def test_mx_hosts_sorted_by_preference(resolver):
    resolver({
        ("agence-x.fr", "MX"): mx((20, "MX2.Agence-X.fr."), (10, "mx1.agence-x.fr.")),
        ("agence-x.fr", "A"): ["192.0.2.1"],
    })
    info = await edns.mx_lookup("Agence-X.FR", use_cache=False)
    assert info.has_mx and info.mx_hosts == ["mx1.agence-x.fr", "mx2.agence-x.fr"]
    assert info.has_a and info.accepts_mail and not info.transient


async def test_nxdomain(resolver):
    r = resolver({("nope.fr", "MX"): dns.resolver.NXDOMAIN()})
    info = await edns.mx_lookup("nope.fr", use_cache=False)
    assert not info.has_mx and not info.has_a and not info.accepts_mail and info.error == "NXDOMAIN"
    assert r.calls == [("nope.fr", "MX")]


async def test_no_mx_but_a_record_is_implicit_mx(resolver):
    resolver({("aonly.fr", "AAAA"): ["2001:db8::1"]})
    info = await edns.mx_lookup("aonly.fr", use_cache=False)
    assert not info.has_mx and info.has_a and info.accepts_mail


async def test_null_mx(resolver):
    resolver({("nomail.fr", "MX"): mx((0, ".")), ("nomail.fr", "A"): ["192.0.2.1"]})
    info = await edns.mx_lookup("nomail.fr", use_cache=False)
    assert info.null_mx and not info.has_mx and not info.accepts_mail


async def test_transient_failure_not_cached(resolver, monkeypatch):
    resolver({("slow.fr", "MX"): dns.exception.Timeout()})
    writes = []

    async def no_cache(_):
        return None

    async def record(info):
        writes.append(info)

    monkeypatch.setattr(edns, "_cache_get", no_cache)
    monkeypatch.setattr(edns, "_cache_put", record)
    info = await edns.mx_lookup("slow.fr")
    assert info.transient and info.error and not info.has_mx
    assert writes == []


async def test_cache_hit_skips_resolver(resolver, monkeypatch):
    r = resolver({})

    async def hit(domain):
        return edns.MxInfo(domain, has_mx=True, mx_hosts=["mx.cached.fr"], cached=True)

    monkeypatch.setattr(edns, "_cache_get", hit)
    info = await edns.mx_lookup("cached.fr")
    assert info.cached and info.mx_hosts == ["mx.cached.fr"] and r.calls == []


async def test_invalid_domain():
    info = await edns.mx_lookup("not a domain")
    assert not info.accepts_mail and info.error == "invalid_domain"
