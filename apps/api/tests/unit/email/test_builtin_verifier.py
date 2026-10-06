"""BuiltinVerifier with mocked MX lookups and a mocked aiosmtplib client (no network)."""

from __future__ import annotations

import re

import aiosmtplib
import pytest

from scout.db.enums import SmtpResult as R
from scout.email.dns import MxInfo
from scout.email.verifier.builtin import BuiltinVerifier, classify_rcpt

from .fakes import FakeMxLookup, FakeSmtpServer

DOMAIN = "agence-x.fr"
TARGET = "marie.dupont@agence-x.fr"
PROBE = re.compile(r"^scout-zz-[0-9a-f]{16}@agence-x\.fr$")


def make(server: FakeSmtpServer | None = None, *, smtp: bool = True, mx: FakeMxLookup | None = None):
    return BuiltinVerifier(
        smtp_enabled=smtp,
        helo_domain="verify.scout.test",
        mail_from="probe@scout.test",
        timeout=2.0,
        mx_lookup=mx or FakeMxLookup(),
        smtp_factory=server or FakeSmtpServer(),
        use_db_cache=False,
    )


async def test_accepted_on_non_catch_all_domain():
    server = FakeSmtpServer(mailboxes={TARGET})
    res = await make(server).verify("Marie.Dupont@Agence-X.fr")
    assert res.address == TARGET and res.syntax_valid and res.mx_valid is True
    assert res.smtp_result == R.accepted and res.catch_all is False
    assert res.verifier == "builtin"
    client = server.clients[0]
    assert client.kwargs["hostname"] == "mx1.agence-x.fr" and client.kwargs["port"] == 25
    assert client.kwargs["local_hostname"] == "verify.scout.test"
    assert client.ehlo_called and client.mail_from == "probe@scout.test" and client.quit_called
    assert client.rcpts[0] == TARGET and PROBE.match(client.rcpts[1])
    assert res.raw["smtp"]["rcpt"][0]["code"] == 250


async def test_rejected_user_unknown():
    res = await make(FakeSmtpServer()).verify(TARGET)
    assert res.smtp_result == R.rejected and res.catch_all is False


async def test_greylisted_is_unknown():
    server = FakeSmtpServer(reply=lambda _: (450, "4.2.0 Greylisted, please try again later"))
    res = await make(server).verify(TARGET)
    assert res.smtp_result == R.unknown and res.catch_all is None


async def test_catch_all_probe_and_memory():
    server = FakeSmtpServer(catch_all=True)
    v = make(server)
    res = await v.verify(TARGET)
    assert res.smtp_result == R.accepted and res.catch_all is True
    assert res.raw["catch_all_source"] == "probe"
    # Second address on the same domain: verdict reused, only the target is probed.
    res2 = await v.verify("jean.martin@agence-x.fr")
    assert res2.catch_all is True and res2.raw["catch_all_source"] == "cache"
    assert server.clients[1].rcpts == ["jean.martin@agence-x.fr"]
    assert await v.is_catch_all(DOMAIN) is True
    assert len(server.clients) == 2  # is_catch_all answered from memory


async def test_is_catch_all_probe_only_random_recipient():
    server = FakeSmtpServer()
    v = make(server)
    assert await v.is_catch_all("Agence-X.fr") is False
    assert len(server.all_rcpts) == 1 and PROBE.match(server.all_rcpts[0])


async def test_smtp_disabled_never_connects():
    server = FakeSmtpServer(mailboxes={TARGET})
    v = make(server, smtp=False)
    res = await v.verify(TARGET)
    assert res.mx_valid is True and res.smtp_result == R.not_attempted and res.catch_all is None
    assert await v.is_catch_all(DOMAIN) is None
    assert server.clients == []


async def test_connection_refused_is_blocked_and_tries_next_mx():
    refused = ConnectionRefusedError(111, "Connection refused")
    server = FakeSmtpServer(connect_errors={"mx1.agence-x.fr": refused, "mx2.agence-x.fr": refused})
    res = await make(server).verify(TARGET)
    assert res.smtp_result == R.blocked and res.catch_all is None and "connect failed" in (res.error or "")
    assert [c.kwargs["hostname"] for c in server.clients] == ["mx1.agence-x.fr", "mx2.agence-x.fr"]


async def test_second_mx_used_when_first_unreachable():
    server = FakeSmtpServer(
        mailboxes={TARGET}, connect_errors={"mx1.agence-x.fr": aiosmtplib.SMTPConnectTimeoutError("timed out")}
    )
    res = await make(server).verify(TARGET)
    assert res.smtp_result == R.accepted and res.raw["smtp"]["host"] == "mx2.agence-x.fr"


async def test_timeout_everywhere_is_timeout():
    err = aiosmtplib.SMTPConnectTimeoutError("timed out")
    server = FakeSmtpServer(connect_errors={"mx1.agence-x.fr": err, "mx2.agence-x.fr": err})
    assert (await make(server).verify(TARGET)).smtp_result == R.timeout


async def test_sender_refused_by_policy_is_blocked():
    server = FakeSmtpServer(mail_reply=(554, "5.7.1 Client host blocked using Spamhaus"))
    res = await make(server).verify(TARGET)
    assert res.smtp_result == R.blocked
    assert server.clients[0].rcpts == []


async def test_dns_outcomes():
    mx = FakeMxLookup(
        {
            "nomx.fr": MxInfo("nomx.fr", has_mx=False, has_a=False, error="NXDOMAIN"),
            "nullmx.fr": MxInfo("nullmx.fr", has_mx=False, has_a=True, null_mx=True, error="null_mx"),
            "aonly.fr": MxInfo("aonly.fr", has_mx=False, has_a=True),
            "flaky.fr": MxInfo("flaky.fr", has_mx=False, error="dns_error:Timeout", transient=True),
        }
    )
    server = FakeSmtpServer(catch_all=True)
    v = make(server, mx=mx)
    assert (await v.verify("a.b@nomx.fr")).mx_valid is False
    assert (await v.verify("a.b@nullmx.fr")).mx_valid is False
    flaky = await v.verify("a.b@flaky.fr")
    assert flaky.mx_valid is None and flaky.smtp_result == R.not_attempted and flaky.error
    aonly = await v.verify("a.b@aonly.fr")
    assert aonly.mx_valid is True and server.clients[-1].kwargs["hostname"] == "aonly.fr"  # implicit MX


async def test_invalid_syntax_and_disposable_skip_dns():
    mx = FakeMxLookup()
    server = FakeSmtpServer()
    v = make(server, mx=mx)
    bad = await v.verify("not an email")
    assert bad.syntax_valid is False and bad.smtp_result == R.not_attempted
    disp = await v.verify("someone@yopmail.com")
    assert disp.disposable and disp.mx_valid is None
    role = await v.verify("contact@gmail.com")
    assert role.role_address and role.free_provider
    assert mx.calls == ["gmail.com"] and len(server.clients) == 1


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (250, "2.1.5 Ok", R.accepted),
        (251, "User not local; will forward", R.accepted),
        (252, "Cannot VRFY user", R.unknown),
        (450, "4.2.0 Greylisted", R.unknown),
        (451, "4.7.1 Try again later", R.unknown),
        (550, "5.1.1 The email account that you tried to reach does not exist", R.rejected),
        (550, "5.4.1 Recipient address rejected: Access denied", R.rejected),
        (551, "User not local", R.rejected),
        (553, "Mailbox name not allowed", R.rejected),
        (554, "Delivery error: dd This user doesn't have an account", R.rejected),
        (550, "5.7.1 Service unavailable; client host blocked using Spamhaus", R.blocked),
        (554, "5.7.606 Access denied, banned sending IP", R.blocked),
        (552, "Requested mail action aborted: exceeded storage allocation", R.unknown),
    ],
)
def test_classify_rcpt(code, message, expected):
    assert classify_rcpt(code, message) == expected
