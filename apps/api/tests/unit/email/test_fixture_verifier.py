"""FixtureVerifier (manifest-driven, test/E2E only) and backend selection."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from scout.config import Settings
from scout.db.enums import SmtpResult as R
from scout.email import verifier as vmod
from scout.email.verifier import fixture as fixture_mod
from scout.email.verifier.builtin import BuiltinVerifier
from scout.email.verifier.fixture import FixtureVerifier
from scout.email.verifier.service import ServiceVerifier

from .fakes import MANIFEST_PATH


@pytest.fixture
def fv() -> FixtureVerifier:
    return FixtureVerifier(MANIFEST_PATH)


async def test_deliverable_iff_listed(fv):
    ok = await fv.verify("Marie.Dupont@agence-x.fr")
    assert ok.smtp_result == R.accepted and ok.catch_all is False and ok.mx_valid is True
    assert ok.verifier == "fixture"
    ko = await fv.verify("m.dupont@agence-x.fr")
    assert ko.smtp_result == R.rejected and ko.catch_all is False


async def test_catch_all_accepts_everything(fv):
    res = await fv.verify("anyone@catchall.fr")
    assert res.smtp_result == R.accepted and res.catch_all is True
    assert await fv.is_catch_all("catchall.fr") is True
    assert await fv.is_catch_all("agence-x.fr") is False


async def test_unknown_and_no_mx_domains(fv):
    assert (await fv.verify("marie@unknown-domain.fr")).mx_valid is False
    assert (await fv.verify("marie@nomx.fr")).mx_valid is False
    assert await fv.is_catch_all("unknown-domain.fr") is None


async def test_smtp_unavailable_domain(fv):
    res = await fv.verify("marie.dupont@nosmtp.fr")
    assert res.mx_valid is True and res.smtp_result == R.not_attempted and res.catch_all is None
    assert await fv.is_catch_all("nosmtp.fr") is None


async def test_flags_and_syntax(fv):
    assert (await fv.verify("bad address")).syntax_valid is False
    role = await fv.verify("contact@agence-x.fr")
    assert role.role_address and role.smtp_result == R.accepted
    assert (await fv.verify("x@yopmail.com")).disposable


def test_refused_in_production(monkeypatch):
    prod = Settings(app_env="production", discovery_fixture_manifest=str(MANIFEST_PATH))
    monkeypatch.setattr(fixture_mod, "get_settings", lambda: prod)
    with pytest.raises(RuntimeError, match="production"):
        FixtureVerifier()


def test_requires_manifest(monkeypatch):
    monkeypatch.setattr(fixture_mod, "get_settings", lambda: Settings(discovery_fixture_manifest=None))
    with pytest.raises(RuntimeError, match="MANIFEST"):
        FixtureVerifier()


def test_build_verifier_backends():
    assert isinstance(vmod.build_verifier(Settings(verifier_backend="builtin")), BuiltinVerifier)
    assert isinstance(
        vmod.build_verifier(Settings(verifier_backend="fixture", discovery_fixture_manifest=str(MANIFEST_PATH))),
        FixtureVerifier,
    )
    auto_service = vmod.build_verifier(
        Settings(verifier_backend="auto", verifier_service_url="http://ev:8080", verifier_service_token=SecretStr("t"))
    )
    assert isinstance(auto_service, ServiceVerifier) and auto_service.base_url == "http://ev:8080"
    assert isinstance(vmod.build_verifier(Settings(verifier_backend="auto", verifier_service_url=None)), BuiltinVerifier)
    assert isinstance(vmod.build_verifier(Settings(verifier_backend="service", verifier_service_url=None)), BuiltinVerifier)


def test_get_and_set_verifier(fv):
    try:
        vmod.set_verifier(fv)
        assert vmod.get_verifier() is fv
        vmod.set_verifier(None)
        assert isinstance(vmod.get_verifier(), BuiltinVerifier)  # tests run with VERIFIER_BACKEND=builtin
    finally:
        vmod.set_verifier(None)
