"""DeepVerifier implementations: Go/AfterShip service folding, factory selection."""

from __future__ import annotations

import json

import httpx

from scout.config import Settings
from scout.db.enums import SmtpResult as R
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
from scout.email.verifier.service import ServiceVerifier


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
