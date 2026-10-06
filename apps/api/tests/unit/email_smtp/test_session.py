"""Batched prober against an in-process SMTP stub on 127.0.0.1 (real aiosmtplib) and a scripted client."""

from __future__ import annotations

import socket
from random import Random

import aiosmtplib
import pytest

from scout.db.enums import MailProvider
from scout.db.enums import SmtpResult as R
from scout.email.contracts import SessionOutcome as S
from scout.email.dns import MxInfo
from scout.email.smtp import session as session_mod
from scout.email.smtp.health import MemoryHealthStore, SmtpHealthMonitor
from scout.email.smtp.session import SmtpProber, identity_problem, probe_summary, restrict_probe
from scout.util.pools import reset_pools

from .smtp_stub import ScriptedFactory, StubSmtpServer

D = "acme.fr"
A, B, C = "anne.martin@acme.fr", "bruno.leroy@acme.fr", "chloe.petit@acme.fr"
HELO = "verify.scout-mail.fr"
FROM = "probe@scout-mail.fr"


@pytest.fixture(autouse=True)
def _reset():
    session_mod.reset_session_state()
    reset_pools()
    yield
    session_mod.reset_session_state()


@pytest.fixture
def mon() -> SmtpHealthMonitor:
    return SmtpHealthMonitor(MemoryHealthStore(), cache_ttl_s=0, enabled=lambda: True)


def prober(mon: SmtpHealthMonitor, *, port: int = 25, factory=None, **kw) -> SmtpProber:
    opts = {
        "enabled": True,
        "helo_domain": HELO,
        "mail_from": FROM,
        "timeout": 2.0,
        "port": port,
        "monitor": mon,
        "mx_spacing_s": 0.0,
        "rng": Random(1),
        "smtp_factory": factory,
        **kw,
    }
    return SmtpProber(**opts)


def window(mon: SmtpHealthMonitor, scope: str = "global") -> list[dict]:
    rec = mon.store.records.get(scope)  # type: ignore[attr-defined]
    return rec.entries if rec else []


# ---- against the asyncio stub server -------------------------------------------------------------


async def test_one_session_batches_targets_and_detects_non_catch_all(mon):
    async with StubSmtpServer(mailboxes={A}) as srv:
        rep = await prober(mon, port=srv.port).probe_domain(D, ["127.0.0.1"], [A, B, C], check_catch_all=True)
    assert rep.session == S.ok and rep.mx_host == "127.0.0.1" and srv.connections == 1
    assert rep.verdicts[A].result == R.accepted and rep.verdicts[A].code == 250
    assert rep.verdicts[B].result == R.rejected and rep.verdicts[C].result == R.rejected
    assert rep.catch_all is False and rep.catch_all_confidence == 0.95
    assert srv.rcpts[:3] == [A, B, C] and len(srv.rcpts) == 5 and rep.probes == 5
    assert set(rep.random_verdicts) == set(srv.rcpts[3:])
    cmds = srv.sessions[0]
    assert cmds[0] == f"EHLO {HELO}" and cmds[1] == f"MAIL FROM:<{FROM}>" and cmds[-1] == "QUIT"
    assert not any(c.upper().startswith("DATA") for c in srv.commands)
    assert [e["o"] for e in window(mon)] == ["ok"]  # one health entry per domain call
    summary = probe_summary(rep)
    assert summary and summary["verdicts"][A]["result"] == "accepted" and summary["connections"] == 1


async def test_targets_are_split_into_connections_of_three(mon):
    targets = [f"user{i}.name@acme.fr" for i in range(7)]
    async with StubSmtpServer() as srv:
        rep = await prober(mon, port=srv.port).probe_domain(D, ["127.0.0.1"], targets, check_catch_all=True)
    assert srv.connections == 3 and rep.connections == 3
    per_session = [sum(c.startswith("RCPT") for c in s) for s in srv.sessions]
    assert per_session == [5, 3, 1]  # 3 targets + 2 random probes, then 3, then 1
    assert all(sum(c.startswith("MAIL") for c in s) == 1 for s in srv.sessions)
    assert all(rep.verdicts[t].result == R.rejected for t in targets)
    assert len(window(mon)) == 1


async def test_catch_all_domain(mon):
    async with StubSmtpServer(rcpt=lambda _: (250, "2.1.5 Ok")) as srv:
        rep = await prober(mon, port=srv.port).probe_domain(D, ["127.0.0.1"], [A], check_catch_all=True)
    assert rep.catch_all is True and rep.catch_all_confidence == 0.95
    assert rep.verdicts[A].result == R.accepted  # raw truth; the engine never calls it SAFE on a catch-all


async def test_greylisting_is_temporary_and_catch_all_unknown(mon):
    grey = (
        450,
        "4.2.0 <x>: Recipient address rejected: Greylisted, see http://postgrey.schweikert.ch/help/acme.fr.html",
    )
    async with StubSmtpServer(rcpt=lambda _: grey) as srv:
        rep = await prober(mon, port=srv.port).probe_domain(D, ["127.0.0.1"], [A, B], check_catch_all=True)
    assert {v.result for v in rep.verdicts.values()} == {R.temporary}
    assert rep.catch_all is None and rep.greylisted and rep.session == S.ok
    assert [e["o"] for e in window(mon)] == ["ok"]  # greylisting says nothing about our infrastructure


async def test_mixed_random_probes_leave_catch_all_unknown(mon):
    def reply(addr: str):
        local = addr.split("@")[0]
        return (250, "Ok") if "." in local else (550, "5.1.1 user unknown")

    async with StubSmtpServer(rcpt=reply) as srv:
        rep = await prober(mon, port=srv.port).probe_domain(D, ["127.0.0.1"], [], check_catch_all=True)
    assert rep.catch_all is None and rep.catch_all_confidence == 0.5 and len(rep.random_verdicts) == 2


async def test_microsoft_dbeb_rejections(mon):
    dbeb = (
        550,
        "5.4.1 Recipient address rejected: Access denied. AS(201806281) [AM0PR02MB1234.prod.outlook.com]",
    )
    async with StubSmtpServer(rcpt=lambda a: (250, "2.1.5 Recipient OK") if a == A else dbeb) as srv:
        rep = await prober(mon, port=srv.port).probe_domain(
            D, ["127.0.0.1"], [A, B], check_catch_all=True, provider=MailProvider.microsoft_365
        )
    assert rep.verdicts[A].result == R.accepted and rep.verdicts[B].result == R.rejected
    assert rep.catch_all is False
    assert [e["o"] for e in window(mon, "provider:microsoft_365")] == ["ok"]


async def test_spamhaus_at_rcpt_is_a_policy_block_never_rejected(mon):
    spamhaus = (554, "5.7.1 Service unavailable; Client host [203.0.113.7] blocked using zen.spamhaus.org")
    async with StubSmtpServer(rcpt=lambda _: spamhaus) as srv:
        rep = await prober(mon, port=srv.port).probe_domain(D, ["127.0.0.1"], [A, B], check_catch_all=True)
    assert rep.session == S.policy_block
    assert {v.result for v in rep.verdicts.values()} == {R.blocked} and rep.catch_all is None
    assert [e["o"] for e in window(mon)] == ["policy_block"]


async def test_greeting_421_is_temporary(mon):
    async with StubSmtpServer(greeting=(421, "4.7.0 Try again later, closing connection.")) as srv:
        rep = await prober(mon, port=srv.port).probe_domain(
            D, ["127.0.0.1"], [A, B, C, "d.e@acme.fr"], check_catch_all=True
        )
    assert rep.session == S.temporary and srv.connections == 1  # no second connection for the 4th target
    assert {v.result for v in rep.verdicts.values()} == {R.temporary}


async def test_greeting_554_is_policy_block(mon):
    async with StubSmtpServer(
        greeting=(554, "5.7.1 Client host [203.0.113.7] blocked using zen.spamhaus.org")
    ) as srv:
        rep = await prober(mon, port=srv.port).probe_domain(D, ["127.0.0.1"], [A], check_catch_all=False)
    assert rep.session == S.policy_block and rep.verdicts[A].result == R.blocked


async def test_421_during_rcpt_defers_the_rest_of_the_domain(mon):
    targets = [A, B, C, "d.e@acme.fr", "f.g@acme.fr"]
    replies = {A: (250, "Ok"), B: (421, "4.7.0 Try again later, closing connection.")}
    async with StubSmtpServer(rcpt=lambda a: replies.get(a, (550, "5.1.1 user unknown"))) as srv:
        rep = await prober(mon, port=srv.port).probe_domain(D, ["127.0.0.1"], targets, check_catch_all=True)
    assert srv.connections == 1 and rep.session == S.ok
    assert rep.verdicts[A].result == R.accepted
    assert all(rep.verdicts[t].result == R.temporary for t in targets[1:])
    assert rep.catch_all is None and any("421" in n for n in rep.notes)


async def test_ehlo_not_understood_falls_back_to_helo(mon):
    async with StubSmtpServer(ehlo=(502, "5.5.2 Error: command not recognized"), mailboxes={A}) as srv:
        rep = await prober(mon, port=srv.port).probe_domain(D, ["127.0.0.1"], [A], check_catch_all=False)
    assert rep.verdicts[A].result == R.accepted
    assert srv.sessions[0][:2] == [f"EHLO {HELO}", f"HELO {HELO}"]


async def test_ehlo_policy_refusal_is_not_retried_with_helo(mon):
    async with StubSmtpServer(
        ehlo=(550, "5.7.1 <verify.scout-mail.fr>: Helo command rejected: Host not found")
    ) as srv:
        rep = await prober(mon, port=srv.port).probe_domain(D, ["127.0.0.1"], [A], check_catch_all=False)
    assert rep.session == S.policy_block and rep.verdicts[A].result == R.blocked
    assert not any(c.startswith("HELO") for c in srv.commands)


async def test_sender_refused_is_policy_block(mon):
    async with StubSmtpServer(
        mail=(553, "5.1.8 <probe@scout-mail.fr>: Sender address rejected: Domain not found")
    ) as srv:
        rep = await prober(mon, port=srv.port).probe_domain(D, ["127.0.0.1"], [A], check_catch_all=True)
    assert rep.session == S.policy_block and rep.verdicts[A].result == R.blocked
    assert srv.rcpts == []


async def test_starttls_failure_reconnects_without_tls_and_is_remembered(mon):
    async with StubSmtpServer(starttls=True, mailboxes={A}) as srv:
        p = prober(mon, port=srv.port)
        rep = await p.probe_domain(D, ["127.0.0.1"], [A], check_catch_all=False)
        assert srv.connections == 2 and "STARTTLS" in srv.sessions[0]
        assert rep.verdicts[A].result == R.accepted and (rep.tls or "").startswith("failed")
        assert any("STARTTLS failed" in n for n in rep.notes)
        rep2 = await p.probe_domain(D, ["127.0.0.1"], [B], check_catch_all=False)
        assert srv.connections == 3 and "STARTTLS" not in srv.sessions[2]
        assert rep2.verdicts[B].result == R.rejected


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_connection_refused_is_infrastructure_not_rejected(mon):
    rep = await prober(mon, port=_free_port()).probe_domain(D, ["127.0.0.1"], [A, B], check_catch_all=True)
    assert rep.session == S.infra_failure and "connect failed" in (rep.error or "")
    assert {v.result for v in rep.verdicts.values()} == {R.blocked}
    assert [e["o"] for e in window(mon)] == ["infra_failure"]


async def test_silent_server_times_out(mon):
    async with StubSmtpServer(greeting_delay_s=3) as srv:
        rep = await prober(mon, port=srv.port, timeout=0.3).probe_domain(
            D, ["127.0.0.1"], [A], check_catch_all=False
        )
    assert rep.session == S.infra_failure and rep.verdicts[A].result == R.timeout


# ---- scripted client -------------------------------------------------------------------------------


async def test_walks_mx_list_only_on_infrastructure_failures(mon):
    f = ScriptedFactory(mailboxes={A}, connect_errors={"mx1.acme.fr": ConnectionRefusedError(111, "refused")})
    rep = await prober(mon, factory=f).probe_domain(
        D, ["mx1.acme.fr", "mx2.acme.fr", "mx3.acme.fr"], [A], check_catch_all=False
    )
    assert f.hosts == ["mx1.acme.fr", "mx2.acme.fr"] and rep.mx_host == "mx2.acme.fr"
    assert rep.hosts_tried == ["mx1.acme.fr", "mx2.acme.fr"] and rep.verdicts[A].result == R.accepted

    f2 = ScriptedFactory(mail_reply=(554, "5.7.1 Client host blocked using zen.spamhaus.org"))
    rep2 = await prober(mon, factory=f2).probe_domain(
        D, ["mx1.acme.fr", "mx2.acme.fr"], [A], check_catch_all=False
    )
    assert f2.hosts == ["mx1.acme.fr"] and rep2.session == S.policy_block  # never hammer the next MX

    timeout = aiosmtplib.SMTPConnectTimeoutError("timed out")
    f3 = ScriptedFactory(
        connect_errors=dict.fromkeys(("mx1.acme.fr", "mx2.acme.fr", "mx3.acme.fr", "mx4.acme.fr"), timeout)
    )
    rep3 = await prober(mon, factory=f3).probe_domain(
        D,
        ["mx1.acme.fr", "mx2.acme.fr", "mx3.acme.fr", "mx4.acme.fr"],
        [A, B, C, "d.e@acme.fr"],
        check_catch_all=True,
    )
    assert f3.hosts == ["mx1.acme.fr", "mx2.acme.fr", "mx3.acme.fr"]  # at most 3 MX, then stop the domain
    assert rep3.session == S.infra_failure and {v.result for v in rep3.verdicts.values()} == {R.timeout}


async def test_scripted_starttls_success_and_failure(mon):
    ok = ScriptedFactory(mailboxes={A}, starttls=True)
    rep = await prober(mon, factory=ok).probe_domain(D, ["mx1.acme.fr"], [A], check_catch_all=False)
    assert rep.tls == "starttls" and ok.clients[0].calls[:4] == ["connect", "ehlo", "starttls", "ehlo"]
    bad = ScriptedFactory(mailboxes={A}, starttls=True, tls_error=OSError("certificate verify failed"))
    rep2 = await prober(mon, factory=bad).probe_domain(D, ["mx9.acme.fr"], [A], check_catch_all=False)
    assert len(bad.clients) == 2 and rep2.verdicts[A].result == R.accepted and rep2.tls == "failed: OSError"


async def test_disabled_and_refused_identity_never_connect(mon):
    f = ScriptedFactory(mailboxes={A})
    off = prober(mon, factory=f, enabled=False)
    rep = await off.probe_domain(D, ["mx1.acme.fr"], [A], check_catch_all=True)
    assert not off.enabled and rep.session == S.not_attempted and rep.verdicts[A].result == R.not_attempted
    bad_id = prober(
        mon,
        factory=f,
        helo_domain="scout.example",
        mail_from="verify@scout.example",
        allow_reserved_identity=False,
    )
    rep2 = await bad_id.probe_domain(D, ["mx1.acme.fr"], [A], check_catch_all=True)
    assert not bad_id.enabled and "identity refused" in (rep2.error or "")
    assert f.clients == [] and window(mon) == []  # not_attempted is never a health failure


@pytest.mark.parametrize(
    ("helo", "mail_from", "ok"),
    [
        ("mail.scout-leads.fr", "verify@scout-leads.fr", True),
        ("scout.example", "verify@scout-leads.fr", False),
        ("mx.local", "verify@scout-leads.fr", False),
        ("localhost", "verify@scout-leads.fr", False),
        ("scout", "verify@scout-leads.fr", False),
        ("203.0.113.7", "verify@scout-leads.fr", False),
        ("mail.scout-leads.fr", "verify@scout.example", False),
        ("mail.scout-leads.fr", "not-an-address", False),
        ("", "verify@scout-leads.fr", False),
    ],
)
def test_identity_validation(helo, mail_from, ok):
    assert (identity_problem(helo, mail_from) is None) == ok


async def test_null_mx_and_implicit_mx(mon):
    async def lookup(domain: str) -> MxInfo:
        if domain == "nullmx.fr":
            return MxInfo(domain, has_mx=False, has_a=True, null_mx=True, error="null_mx")
        return MxInfo(domain, has_mx=False, has_a=True)

    f = ScriptedFactory(catch_all=True)
    p = prober(mon, factory=f, mx_lookup=lookup)
    rep = await p.probe_domain("nullmx.fr", [], ["a.b@nullmx.fr"], check_catch_all=True)
    assert rep.session == S.not_attempted and "RFC 7505" in (rep.error or "") and f.clients == []
    rep2 = await p.probe_domain("aonly.fr", [], ["a.b@aonly.fr"], check_catch_all=False)
    assert f.hosts == ["aonly.fr"] and rep2.verdicts["a.b@aonly.fr"].result == R.accepted


async def test_secure_gateway_accepts_are_uninformative(mon):
    gw = ["eu-smtp-inbound-1.mimecast.com"]
    f = ScriptedFactory(catch_all=True)
    rep = await prober(mon, factory=f).probe_domain(D, gw, [A], check_catch_all=False)
    assert len(f.clients[0].rcpts) == 3  # random probes forced even though catch-all was "known"
    assert rep.uninformative_accepts and rep.catch_all is None and rep.catch_all_confidence == 0.3
    assert rep.verdicts[A].result == R.unknown and "uninformative" in rep.verdicts[A].message
    # a gateway doing recipient validation (random probes rejected) is informative again
    f2 = ScriptedFactory(mailboxes={A})
    rep2 = await prober(mon, factory=f2).probe_domain(D, gw, [A], check_catch_all=False)
    assert rep2.catch_all is False and rep2.verdicts[A].result == R.accepted
    yahoo = await prober(mon, factory=ScriptedFactory(catch_all=True)).probe_domain(
        "yahoo-hosted.fr", ["mta5.am0.yahoodns.net"], ["x.y@yahoo-hosted.fr"], check_catch_all=True
    )
    assert yahoo.verdicts["x.y@yahoo-hosted.fr"].result == R.unknown and yahoo.catch_all is None


async def test_reuses_given_catch_all_addresses(mon):
    f = ScriptedFactory()
    given = ["maelis.torvane@acme.fr", "kvarelton@acme.fr"]
    rep = await prober(mon, factory=f).probe_domain(
        D, ["mx1.acme.fr"], [A], check_catch_all=True, catch_all_addresses=given
    )
    assert f.clients[0].rcpts == [A, *given] and list(rep.random_verdicts) == given


async def test_foreign_addresses_are_not_probed(mon):
    f = ScriptedFactory(catch_all=True)
    rep = await prober(mon, factory=f).probe_domain(
        D, ["mx1.acme.fr"], [A, "x@other.fr", "garbage"], check_catch_all=False
    )
    assert f.clients[0].rcpts == [A]
    assert rep.verdicts["x@other.fr"].result == R.not_attempted


async def test_time_budget_defers_remaining_connections(mon):
    f = ScriptedFactory()
    targets = [f"u{i}.x@acme.fr" for i in range(7)]
    rep = await prober(mon, factory=f, time_budget_s=0.0).probe_domain(
        D, ["mx1.acme.fr"], targets, check_catch_all=False
    )
    assert len(f.clients) == 1
    assert [rep.verdicts[t].result for t in targets] == [R.rejected] * 3 + [R.temporary] * 4


async def test_mid_rcpt_disconnect_keeps_answered_targets(mon):
    f = ScriptedFactory(mailboxes={A})
    f.rcpt_errors = []

    def reply(addr: str):
        if addr == B:
            raise aiosmtplib.SMTPServerDisconnected("Connection lost")
        return (250, "Ok") if addr == A else (550, "5.1.1 user unknown")

    f._reply = reply  # type: ignore[assignment]
    rep = await prober(mon, factory=f).probe_domain(D, ["mx1.acme.fr"], [A, B, C], check_catch_all=False)
    assert rep.session == S.ok and rep.verdicts[A].result == R.accepted
    assert rep.verdicts[B].result == R.unknown and rep.verdicts[C].result == R.unknown


async def test_restrict_probe_view_and_module_level_entry_point(mon):
    f = ScriptedFactory(mailboxes={A})
    session_mod.set_prober(prober(mon, factory=f))
    try:
        rep = await session_mod.probe_domain(D, ["mx1.acme.fr"], [A, B], check_catch_all=False)
    finally:
        session_mod.set_prober(None)
    view = restrict_probe(rep, [B, "zzz@acme.fr"])
    assert list(view.verdicts) == [B] and view.probes == 1 and view.session == rep.session
