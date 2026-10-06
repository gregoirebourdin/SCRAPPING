"""ServiceVerifier: JSON mapping, auth header and builtin fallback (respx-mocked HTTP)."""

from __future__ import annotations

import httpx
import pytest
import respx

from scout.db.enums import SmtpResult as R
from scout.email import dns as edns
from scout.email.dns import MxInfo
from scout.email.verifier.service import ServiceVerifier

from .fakes import vr

BASE = "http://email-verifier.railway.internal:8080"


def payload(email="marie.dupont@agence-x.fr", *, smtp=None, **over):
    body = {
        "email": email,
        "syntax_valid": True,
        "has_mx": True,
        "mx_hosts": ["mx1.agence-x.fr"],
        "smtp": {
            "enabled": True,
            "host_exists": True,
            "deliverable": False,
            "full_inbox": False,
            "catch_all": False,
            "disabled": False,
            "error": None,
            **(smtp or {}),
        },
        "disposable": False,
        "role_account": False,
        "free": False,
        "reachable": "unknown",
        "error": None,
    }
    body.update(over)
    return body


class FallbackStub:
    name = "builtin"

    def __init__(self):
        self.calls = []

    async def verify(self, address):
        self.calls.append(address)
        return vr(address, smtp=R.not_attempted)

    async def is_catch_all(self, domain):
        self.calls.append(domain)
        return None


@pytest.fixture
def fallback():
    return FallbackStub()


@pytest.fixture
def sv(fallback):
    return ServiceVerifier(BASE, "s3cret", fallback=fallback, use_db_cache=False)


@respx.mock
async def test_deliverable_maps_to_accepted_with_bearer(sv):
    route = respx.post(f"{BASE}/v1/verify").mock(
        return_value=httpx.Response(200, json=payload(smtp={"deliverable": True}, reachable="yes"))
    )
    res = await sv.verify("Marie.Dupont@agence-x.fr")
    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer s3cret"
    assert req.content == b'{"email":"marie.dupont@agence-x.fr"}'
    assert res.verifier == "aftership" and res.smtp_result == R.accepted and res.catch_all is False
    assert res.mx_valid is True and res.raw["mx_hosts"] == ["mx1.agence-x.fr"]


@pytest.mark.parametrize(
    ("smtp", "expected", "catch_all"),
    [
        ({"catch_all": True}, R.unknown, True),
        ({"enabled": False, "catch_all": None, "host_exists": False}, R.not_attempted, None),
        ({"error": "The connection to the mail server has timed out", "catch_all": None}, R.timeout, None),
        ({"error": "dial tcp: connection refused", "host_exists": False, "catch_all": None}, R.blocked, None),
        ({"error": "Blocked by mail server", "host_exists": True, "catch_all": None}, R.unknown, None),
        ({"full_inbox": True}, R.unknown, False),
        ({}, R.rejected, False),
    ],
)
@respx.mock
async def test_smtp_mapping(sv, smtp, expected, catch_all):
    respx.post(f"{BASE}/v1/verify").mock(return_value=httpx.Response(200, json=payload(smtp=smtp)))
    res = await sv.verify("marie.dupont@agence-x.fr")
    assert res.smtp_result == expected and res.catch_all is catch_all


@respx.mock
async def test_flags_are_complemented_by_local_lists(sv):
    respx.post(f"{BASE}/v1/verify").mock(
        return_value=httpx.Response(200, json=payload("contact@orange.fr", smtp={"enabled": False}))
    )
    res = await sv.verify("contact@orange.fr")
    assert res.free_provider and res.role_address  # AfterShip flags were False


@respx.mock
async def test_invalid_syntax_and_disposable(sv):
    respx.post(f"{BASE}/v1/verify").mock(
        side_effect=[
            httpx.Response(
                200, json=payload("bad", syntax_valid=False, has_mx=False, smtp={"enabled": False})
            ),
            httpx.Response(
                200, json=payload("x@yopmail.com", disposable=True, has_mx=False, smtp={"enabled": False})
            ),
        ]
    )
    bad = await sv.verify("bad")
    assert bad.syntax_valid is False and bad.smtp_result == R.not_attempted and bad.mx_valid is None
    disp = await sv.verify("x@yopmail.com")
    assert disp.disposable and disp.mx_valid is None


@respx.mock
async def test_no_mx_confirmed_locally_with_a_record(sv, monkeypatch):
    lookups = []

    async def fake_lookup(domain, use_cache=True):
        lookups.append(domain)
        return MxInfo(domain, has_mx=False, has_a=domain == "aonly.fr")

    monkeypatch.setattr(edns, "mx_lookup", fake_lookup)
    respx.post(f"{BASE}/v1/verify").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json=payload(
                req.read().decode().split('"')[3],
                has_mx=False,
                error="lookup: no such host",
                smtp={"enabled": False},
            ),
        )
    )
    assert (await sv.verify("a.b@aonly.fr")).mx_valid is True
    assert (await sv.verify("a.b@nothing.fr")).mx_valid is False
    assert lookups == ["aonly.fr", "nothing.fr"]


@respx.mock
async def test_dns_error_is_not_a_definitive_no_mx(sv):
    respx.post(f"{BASE}/v1/verify").mock(
        return_value=httpx.Response(
            200, json=payload(has_mx=False, error="i/o timeout", smtp={"enabled": False})
        )
    )
    assert (await sv.verify("marie.dupont@agence-x.fr")).mx_valid is None


@pytest.mark.parametrize(
    "side_effect",
    [
        httpx.Response(500, json={"error": "boom"}),
        httpx.ConnectError("refused"),
        httpx.Response(200, text="nope"),
    ],
    ids=["http_500", "connect_error", "bad_json"],
)
@respx.mock
async def test_falls_back_to_builtin(sv, fallback, side_effect):
    respx.post(f"{BASE}/v1/verify").mock(side_effect=[side_effect])
    res = await sv.verify("marie.dupont@agence-x.fr")
    assert fallback.calls == ["marie.dupont@agence-x.fr"]
    assert res.verifier == "fake" and "service_error" in res.raw


@respx.mock
async def test_is_catch_all(sv, fallback):
    respx.post(f"{BASE}/v1/catch-all").mock(
        side_effect=[
            httpx.Response(200, json={"domain": "agence-x.fr", "catch_all": True, "smtp_enabled": True}),
            httpx.Response(200, json={"domain": "agence-x.fr", "catch_all": None, "smtp_enabled": False}),
            httpx.Response(503, json={"error": "busy"}),
        ]
    )
    assert await sv.is_catch_all("Agence-X.fr") is True
    assert await sv.is_catch_all("agence-x.fr") is None
    assert await sv.is_catch_all("agence-x.fr") is None and fallback.calls == ["agence-x.fr"]


async def test_no_token_means_no_auth_header():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=payload(smtp={"deliverable": True}))

    sv = ServiceVerifier(BASE, None, transport=httpx.MockTransport(handler), use_db_cache=False)
    assert (await sv.verify("marie.dupont@agence-x.fr")).smtp_result == R.accepted
