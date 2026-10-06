"""DeepVerifier implementations: Go/AfterShip service folding, factory selection."""

from __future__ import annotations

import json

import httpx

from scout.config import Settings
from scout.db.enums import SmtpResult as R
from scout.email.contracts import DomainProbeResult
from scout.email.contracts import SessionOutcome as S
from scout.email.smtp import deep_verifiers as dv
from scout.email.smtp.deep_verifiers import (
    BuiltinDeepVerifier,
    DeepVerifier,
    ServiceDeepVerifier,
    WorldDeepVerifier,
    build_deep_verifier,
)
from scout.email.smtp.health import MemoryHealthStore, SmtpHealthMonitor
from scout.email.smtp.world import MailWorld
from scout.email.types import VerificationResult
from scout.email.verifier.service import Breaker, ServiceVerifier, shared_breaker, unreachable


def service_payload(email: str) -> dict:
    local = email.split("@")[0]
    smtp: dict = {
        "enabled": True,
        "host_exists": True,
        "catch_all": False,
        "deliverable": local == "anne.martin",
    }
    if local == "grey":
        smtp = {
            "enabled": True,
            "host_exists": True,
            "error": "450 4.2.0 Recipient address rejected: Greylisted",
        }
    if local == "rbl":
        smtp = {
            "enabled": True,
            "host_exists": True,
            "error": "554 5.7.1 Client host blocked using zen.spamhaus.org",
        }
    return {"email": email, "syntax_valid": True, "has_mx": True, "smtp": smtp}


def make_service(catch_all: bool | None = False) -> tuple[ServiceVerifier, list[str]]:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(request.url.path)
        if request.url.path == "/v1/catch-all":
            return httpx.Response(200, json={"catch_all": catch_all})
        return httpx.Response(200, json=service_payload(body["email"]))

    svc = ServiceVerifier(
        "http://verifier.test", "tok", transport=httpx.MockTransport(handler), use_db_cache=False
    )
    return svc, calls


async def test_service_deep_verifier_folds_per_address_results():
    svc, calls = make_service()
    mon = SmtpHealthMonitor(MemoryHealthStore(), cache_ttl_s=0, enabled=lambda: True)
    v = ServiceDeepVerifier(svc, monitor=mon)
    rep = await v.probe_domain(
        "acme.fr",
        ["mx1.acme.fr"],
        ["anne.martin@acme.fr", "a.martin@acme.fr", "grey@acme.fr", "rbl@acme.fr", "x@other.fr"],
        check_catch_all=True,
    )
    assert rep.verifier == "aftership" and rep.session == S.ok
    assert rep.verdicts["anne.martin@acme.fr"].result == R.accepted
    assert rep.verdicts["a.martin@acme.fr"].result == R.rejected
    assert rep.verdicts["grey@acme.fr"].result == R.temporary  # re-read by our classifier
    assert rep.verdicts["rbl@acme.fr"].result == R.blocked
    assert rep.verdicts["x@other.fr"].result == R.not_attempted
    assert rep.catch_all is False and calls.count("/v1/catch-all") == 1 and calls.count("/v1/verify") == 4
    assert [e["o"] for e in mon.store.records["global"].entries] == ["ok"]  # type: ignore[attr-defined]


async def test_service_deep_verifier_gateway_accepts_are_uninformative():
    svc, _ = make_service(catch_all=True)  # the gateway accepts random recipients too
    v = ServiceDeepVerifier(svc, record_health=False)
    rep = await v.probe_domain(
        "gw.fr", ["eu-smtp-inbound-1.mimecast.com"], ["anne.martin@gw.fr"], check_catch_all=True
    )
    assert rep.verdicts["anne.martin@gw.fr"].result == R.unknown and rep.catch_all is None
    assert rep.uninformative_accepts


def test_protocol_and_factory():
    world = MailWorld()
    assert isinstance(WorldDeepVerifier(world), DeepVerifier)
    assert isinstance(BuiltinDeepVerifier(enabled=False), DeepVerifier)
    assert isinstance(build_deep_verifier(Settings(verifier_backend="builtin")), BuiltinDeepVerifier)
    assert isinstance(build_deep_verifier(Settings(verifier_backend="auto")), BuiltinDeepVerifier)
    svc = build_deep_verifier(Settings(verifier_backend="auto", verifier_service_url="http://verifier.test"))
    assert isinstance(svc, ServiceDeepVerifier)
    dv.set_default_world(world)
    try:
        fixture = build_deep_verifier(Settings(verifier_backend="fixture"))
        assert isinstance(fixture, WorldDeepVerifier) and fixture.world is world
        dv.set_deep_verifier(None)
        assert isinstance(dv.get_deep_verifier(), BuiltinDeepVerifier)  # test env default backend
    finally:
        dv.set_default_world(None)
        dv.set_deep_verifier(None)


# ---- port 25 unreachable from the service host (e.g. the provider still blocks it) ----------------

BLOCKED = "The connection to the mail server has timed out : dial tcp 109.234.161.192:25: i/o timeout"


class LocalVerifier:
    """Stands for this host's own SMTP verifier."""

    name = "builtin"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def verify(self, address: str) -> VerificationResult:
        self.calls.append(address)
        return VerificationResult(
            address=address,
            syntax_valid=True,
            mx_valid=True,
            smtp_result=R.accepted,
            catch_all=False,
            disposable=False,
            role_address=False,
            free_provider=False,
            verifier=self.name,
        )

    async def is_catch_all(self, domain: str) -> bool | None:
        self.calls.append(domain)
        return False


class LocalDeepVerifier:
    name = "builtin"
    enabled = True

    def __init__(self) -> None:
        self.domains: list[str] = []

    async def probe_domain(self, domain, mx_hosts, addresses, **kw) -> DomainProbeResult:
        self.domains.append(domain)
        return DomainProbeResult(domain=domain, session=S.ok, verifier=self.name)


def blocked_service(breaker: Breaker) -> tuple[ServiceVerifier, list[str], LocalVerifier]:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/catch-all":
            return httpx.Response(200, json={"domain": "acme.fr", "catch_all": None, "error": BLOCKED})
        email = json.loads(request.content)["email"]
        smtp = {"enabled": True, "host_exists": False, "deliverable": False, "error": BLOCKED}
        return httpx.Response(200, json={"email": email, "syntax_valid": True, "has_mx": True, "smtp": smtp})

    local = LocalVerifier()
    svc = ServiceVerifier(
        "http://verifier.test",
        "tok",
        transport=httpx.MockTransport(handler),
        use_db_cache=False,
        fallback=local,
        breaker=breaker,
    )
    return svc, calls, local


async def test_unreachable_mail_server_falls_back_then_skips_the_service_for_a_while():
    now = [0.0]
    svc, calls, local = blocked_service(Breaker(trip_after=3, cooldown_s=600, clock=lambda: now[0]))
    first = await svc.verify("anne.martin@acme.fr")
    assert first.verifier == "builtin" and first.smtp_result == R.accepted  # not a 10 s "timeout" verdict
    assert "could not reach the mail server" in first.raw["service_error"]
    await svc.verify("b@acme.fr")
    assert await svc.is_catch_all("acme.fr") is False  # third miss: the breaker trips
    assert calls == ["/v1/verify", "/v1/verify", "/v1/catch-all"]
    await svc.verify("c@acme.fr")
    assert len(calls) == 3 and local.calls[-1] == "c@acme.fr"  # skipped: straight to this host
    now[0] = 601.0
    await svc.verify("d@acme.fr")
    assert len(calls) == 4  # cooldown over: the service is tried again (port 25 may be open by now)


def test_unreachable_is_only_a_failed_connection():
    assert unreachable(BLOCKED)
    assert unreachable("dial tcp 1.2.3.4:25: connect: connection refused")
    assert not unreachable("550 5.1.1 user unknown")
    assert not unreachable("dial tcp 1.2.3.4:443: i/o timeout") and not unreachable(None)


async def test_deep_verifier_sends_whole_domains_to_the_fallback_while_the_service_is_skipped():
    breaker = Breaker(trip_after=1, cooldown_s=600)
    svc, calls, _ = blocked_service(breaker)
    deep_fallback = LocalDeepVerifier()
    v = ServiceDeepVerifier(svc, record_health=False, fallback=deep_fallback)
    await v.probe_domain("acme.fr", ["mx.acme.fr"], ["a@acme.fr"], check_catch_all=False)
    assert breaker.open() and calls == ["/v1/verify"]
    rep = await v.probe_domain("beta.fr", ["mx.beta.fr"], ["a@beta.fr"], check_catch_all=True)
    assert rep.verifier == "builtin" and deep_fallback.domains == ["beta.fr"] and calls == ["/v1/verify"]


def test_fast_and_deep_paths_share_one_breaker_per_service():
    from scout.email.verifier import build_verifier

    s = Settings(verifier_backend="auto", verifier_service_url="http://shared.test")
    deep = build_deep_verifier(s)
    fast = build_verifier(s)
    assert isinstance(deep, ServiceDeepVerifier) and isinstance(fast, ServiceVerifier)
    assert deep.service.breaker is fast.breaker is shared_breaker("http://shared.test/")
    assert isinstance(deep.fallback, BuiltinDeepVerifier)
