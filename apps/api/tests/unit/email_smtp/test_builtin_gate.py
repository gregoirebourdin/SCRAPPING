"""Legacy BuiltinVerifier.verify(address): reuses the batched prober and honours the SMTP health gate."""

from __future__ import annotations

from scout.db.enums import SmtpHealthState as H
from scout.db.enums import SmtpResult as R
from scout.email.contracts import SessionOutcome as S
from scout.email.smtp import session as session_mod
from scout.email.smtp.health import MemoryHealthStore, SmtpHealthMonitor
from scout.email.verifier.builtin import BuiltinVerifier, random_probe_address

from ..email.fakes import FakeMxLookup, FakeSmtpServer

TARGET = "marie.dupont@agence-x.fr"


def make(server: FakeSmtpServer, mon: SmtpHealthMonitor | None = None, **kw) -> BuiltinVerifier:
    session_mod.reset_session_state()
    return BuiltinVerifier(
        smtp_enabled=True,
        helo_domain="verify.scout-mail.fr",
        mail_from="probe@scout-mail.fr",
        timeout=2.0,
        mx_lookup=FakeMxLookup(),
        smtp_factory=server,
        use_db_cache=False,
        health_monitor=mon,
        **kw,
    )


async def blocked_monitor() -> SmtpHealthMonitor:
    mon = SmtpHealthMonitor(MemoryHealthStore(), cache_ttl_s=0, enabled=lambda: True)
    for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr"):
        await mon.record_session(None, S.infra_failure, domain=d)
    assert await mon.current_state() == H.BLOCKED
    return mon


async def test_blocked_health_means_not_attempted_never_rejected():
    server = FakeSmtpServer()  # would reject everything as user unknown
    v = make(server, await blocked_monitor())
    res = await v.verify(TARGET)
    assert res.mx_valid is True and res.smtp_result == R.not_attempted
    assert res.raw["smtp"]["health"] == "BLOCKED" and "health gate closed" in (res.error or "")
    assert await v.is_catch_all("agence-x.fr") is None
    assert server.clients == []


async def test_failures_feed_the_health_monitor_of_the_verifier():
    refused = ConnectionRefusedError(111, "refused")
    errors = {f"mx{i}.{d}": refused for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr") for i in (1, 2)}
    server = FakeSmtpServer(connect_errors=errors)
    v = make(server)
    for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr"):
        res = await v.verify(f"x.y@{d}")
        assert res.smtp_result == R.blocked  # infrastructure, never rejected
    assert await v.monitor.current_state() == H.BLOCKED
    calls = len(server.clients)
    res = await v.verify("x.y@f.fr")
    assert res.smtp_result == R.not_attempted and len(server.clients) == calls


async def test_refused_identity_outside_tests_disables_smtp():
    server = FakeSmtpServer(mailboxes={TARGET})
    session_mod.reset_session_state()
    v = BuiltinVerifier(
        smtp_enabled=True,
        helo_domain="scout.example",
        mail_from="verify@scout.example",
        mx_lookup=FakeMxLookup(),
        smtp_factory=server,
        use_db_cache=False,
    )
    v.prober.allow_reserved_identity = False
    res = await v.verify(TARGET)
    assert res.smtp_result == R.not_attempted and "identity refused" in (res.error or "")
    assert server.clients == []


def test_random_probe_address_is_plausible():
    addr = random_probe_address("acme.fr")
    assert addr.endswith("@acme.fr") and "scout" not in addr and "test" not in addr
