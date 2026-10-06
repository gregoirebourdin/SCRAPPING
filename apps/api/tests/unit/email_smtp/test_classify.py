"""SMTP reply classification: RFC 3463 first, wording second; infrastructure is never "invalid"."""

from __future__ import annotations

import re
import socket
from random import Random

import aiosmtplib
import pytest

from scout.db.enums import MailProvider
from scout.db.enums import SmtpResult as R
from scout.email.contracts import SessionOutcome as S
from scout.email.smtp.classify import (
    ReplyKind,
    classify_exception,
    classify_rcpt,
    classify_rcpt_reply,
    classify_session_reply,
    is_greylisting,
    is_policy_reply,
    parse_enhanced,
    provider_from_mx,
    random_local_part,
    random_probe_addresses,
    uninformative_accepts,
)

# (code, message, expected result) — real-world reply strings.
RCPT_TABLE = [
    # ---- accepted ---------------------------------------------------------------------------
    (250, "2.1.5 Ok", R.accepted),
    (250, "2.1.5 Recipient OK", R.accepted),
    (251, "User not local; will forward to <x@y>", R.accepted),
    (252, "2.1.5 Cannot VRFY user, but will accept message", R.unknown),
    # ---- temporary: every 4xx ---------------------------------------------------------------
    (
        450,
        "4.2.0 <x@acme.fr>: Recipient address rejected: Greylisted, see http://postgrey.schweikert.ch/help/acme.fr.html",
        R.temporary,
    ),
    (451, "4.7.1 Please try again later", R.temporary),
    (421, "4.7.0 Try again later, closing connection.", R.temporary),
    (450, "4.7.1 Client host rejected: cannot find your reverse hostname, [203.0.113.7]", R.temporary),
    (451, "4.3.0 Temporary lookup failure", R.temporary),
    (452, "4.5.3 Too many recipients", R.temporary),
    (452, "4.2.2 The email account that you tried to reach is over quota.", R.temporary),
    (451, "4.7.500 Server busy. Please try again later from [203.0.113.7]. (AS800)", R.temporary),
    (450, "4.1.8 <verify@scout.fr>: Sender address rejected: Domain not found", R.temporary),
    (421, "4.7.28 Our system has detected an unusual rate of unsolicited mail", R.temporary),
    # ---- rejected: user unknown (enhanced code) ---------------------------------------------
    (
        550,
        "5.1.1 The email account that you tried to reach does not exist. Please try double-checking the recipient's email address for typos or unnecessary spaces. For more information, go to https://support.google.com/mail/?p=NoSuchUser - gsmtp",
        R.rejected,
    ),
    (550, "550-5.1.1 The email account that you tried to reach does not exist", R.rejected),
    (550, "5.1.1 <x@acme.fr>: Recipient address rejected: User unknown in virtual mailbox table", R.rejected),
    (550, "5.1.1 <x@acme.fr>: Recipient address rejected: User unknown in relay recipient table", R.rejected),
    (550, "5.1.10 RESOLVER.ADR.RecipientNotFound; Recipient not found by SMTP address lookup", R.rejected),
    (550, "5.1.0 Address rejected.", R.rejected),
    (553, "5.1.3 Bad recipient address syntax", R.rejected),
    (550, "5.2.1 The email account that you tried to reach is disabled.", R.rejected),
    # Microsoft 365 Directory-Based Edge Blocking: exact 5.4.1 recipient-rejected form only.
    (
        550,
        "5.4.1 Recipient address rejected: Access denied. AS(201806281) [AM0PR02MB1234.eurprd02.prod.outlook.com]",
        R.rejected,
    ),
    (
        550,
        "5.4.1 Recipient address rejected: Access denied. For more information see https://aka.ms/EXOSmtpErrors",
        R.rejected,
    ),
    # ---- rejected: user unknown (wording only) ------------------------------------------------
    (550, "No Such User Here", R.rejected),
    (550, "Requested action not taken: mailbox unavailable", R.rejected),
    (550, "5.0.0 Requested action not taken: mailbox unavailable", R.rejected),
    (
        554,
        "delivery error: dd This user doesn't have a yahoo.com account (x@yahoo.com) [0] - mta1234.mail.bf1.yahoo.com",
        R.rejected,
    ),
    (551, "User not local; please try <x@other>", R.rejected),
    (553, "Mailbox name not allowed", R.rejected),
    (550, "Recipient not found", R.rejected),
    (550, "Invalid recipient <x@acme.fr>", R.rejected),
    # ---- mailbox full: the mailbox exists -----------------------------------------------------
    (
        552,
        "5.2.2 The email account that you tried to reach is over quota. Please direct the recipient to https://support.google.com/mail/?p=OverQuotaPerm",
        R.accepted,
    ),
    (552, "Requested mail action aborted: exceeded storage allocation", R.accepted),
    (552, "5.3.4 Message size exceeds fixed maximum message size", R.unknown),
    # ---- blocked: our IP / identity / policy (never rejected) ---------------------------------
    (
        554,
        "5.7.1 Service unavailable; Client host [203.0.113.7] blocked using zen.spamhaus.org; https://www.spamhaus.org/query/ip/203.0.113.7",
        R.blocked,
    ),
    (554, "Service unavailable; Client host [203.0.113.7] blocked using zen.spamhaus.org", R.blocked),
    (
        550,
        "5.7.606 Access denied, banned sending IP [203.0.113.7]. To request removal from this list please visit https://sender.office.com/",
        R.blocked,
    ),
    (550, "5.7.708 Service unavailable. Access denied, traffic not accepted from this IP.", R.blocked),
    (
        550,
        "5.7.1 [203.0.113.7] Our system has detected an unusual rate of unsolicited mail originating from your IP address.",
        R.blocked,
    ),
    (550, "5.7.1 <x@acme.fr>: Recipient address rejected: Access denied", R.blocked),
    (550, "5.7.1 Client host rejected: cannot find your reverse hostname, [203.0.113.7]", R.blocked),
    (554, "Client host rejected: cannot find your hostname, [203.0.113.7]", R.blocked),
    (504, "5.5.2 <scout>: Helo command rejected: need fully-qualified hostname", R.blocked),
    (550, "5.1.7 <verify@scout.fr>: Sender address rejected: undeliverable address", R.blocked),
    (
        550,
        "5.1.0 <verify@scout.fr>: Sender address rejected: User unknown in local recipient table",
        R.blocked,
    ),
    (553, "5.1.8 <verify@scout.fr>: Sender address rejected: Domain not found", R.blocked),
    (554, "Relay access denied", R.blocked),
    (550, "5.7.1 Unable to relay", R.blocked),
    (553, "sorry, that domain isn't in my list of allowed rcpthosts (#5.7.1)", R.blocked),
    (550, "Administrative prohibition", R.blocked),
    (
        554,
        "Your access to this mail system has been rejected due to the sending MTA's poor reputation.",
        R.blocked,
    ),
    (550, "IP 203.0.113.7 is listed on bl.spamcop.net", R.blocked),
    (550, "Recipient address rejected: Access denied", R.blocked),  # no enhanced code: policy wording wins
    (554, "5.7.9 Message not accepted for policy reasons.", R.blocked),
    # ---- ambiguous → unknown (never rejected) -------------------------------------------------
    (550, "", R.unknown),
    (550, "5.0.0", R.unknown),
    (554, "Transaction failed", R.unknown),
    (550, "Unrouteable address", R.unknown),
    (550, "5.1.2 Host unknown", R.unknown),
    (554, "5.4.6 Routing loop detected", R.unknown),
    (550, "5.4.1 No answer from host", R.unknown),
    (550, "Too many invalid recipients, try again later", R.temporary),
    (503, "5.5.1 Error: need MAIL command", R.unknown),
]


@pytest.mark.parametrize(("code", "message", "expected"), RCPT_TABLE)
def test_rcpt_classification_table(code, message, expected):
    assert classify_rcpt(code, message) == expected


def test_policy_and_temporary_replies_are_never_rejected():
    for code, message, expected in RCPT_TABLE:
        cls = classify_rcpt_reply(code, message)
        if 400 <= code < 500:
            assert cls.result == R.temporary, message
        if is_policy_reply(code, message):
            assert cls.result != R.rejected, message


def test_reply_kinds_carry_the_reason():
    grey = classify_rcpt_reply(450, "4.2.0 <x>: Recipient address rejected: Greylisted")
    assert grey.kind == ReplyKind.greylisted and grey.enhanced == "4.2.0"
    full = classify_rcpt_reply(552, "5.2.2 mailbox full")
    assert full.kind == ReplyKind.mailbox_full and full.result == R.accepted
    dbeb = classify_rcpt_reply(550, "5.4.1 Recipient address rejected: Access denied. AS(201806281)")
    assert dbeb.kind == ReplyKind.directory_reject and dbeb.is_mailbox_verdict
    sender = classify_rcpt_reply(550, "5.1.7 <me@scout.fr>: Sender address rejected: undeliverable address")
    assert sender.kind == ReplyKind.sender_rejected and not sender.is_mailbox_verdict
    rate = classify_rcpt_reply(421, "4.7.0 Try again later, closing connection.")
    assert rate.kind == ReplyKind.rate_limited


def test_bare_550_on_microsoft_365_is_inconclusive():
    cls = classify_rcpt_reply(550, "Requested action not taken", provider=MailProvider.microsoft_365)
    assert cls.result == R.unknown and "Microsoft 365" in cls.reason


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("5.1.1 user unknown", "5.1.1"),
        ("550-5.1.1 The email account", "5.1.1"),
        ("#5.7.1 not in rcpthosts", "5.7.1"),
        ("5.1.10 RESOLVER.ADR.RecipientNotFound", "5.1.10"),
        ("blocked [192.0.2.1]", None),  # IP addresses are not status codes
        ("Client host [45.4.1.20] rejected", None),
        ("from [4.2.2.1]", None),
        ("does not exist 5.1.1.", "5.1.1"),
        ("", None),
    ],
)
def test_parse_enhanced(message, expected):
    assert parse_enhanced(message) == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (220, S.ok),
        (250, S.ok),
        (354, S.ok),
        (421, S.temporary),
        (450, S.temporary),
        (554, S.policy_block),
        (502, S.policy_block),
        (999, S.infra_failure),
    ],
)
def test_session_reply(code, expected):
    assert classify_session_reply(code, "x") == expected


def test_session_exceptions():
    timeout = classify_exception(aiosmtplib.SMTPConnectTimeoutError("timed out"))
    assert (timeout.outcome, timeout.result) == (S.infra_failure, R.timeout) and "timeout" in timeout.detail
    refused = classify_exception(ConnectionRefusedError(111, "Connection refused"))
    assert (refused.outcome, refused.result) == (
        S.infra_failure,
        R.blocked,
    ) and "connect failed" in refused.detail
    dns_fail = classify_exception(socket.gaierror(-2, "Name or service not known"))
    assert dns_fail.outcome == S.infra_failure
    dropped = classify_exception(aiosmtplib.SMTPServerDisconnected("Unexpected EOF received"))
    assert (dropped.outcome, dropped.result) == (S.infra_failure, R.unknown)
    greet_421 = classify_exception(aiosmtplib.SMTPConnectResponseError(421, "4.7.0 Try again later"))
    assert (greet_421.outcome, greet_421.result) == (S.temporary, R.temporary)
    greet_554 = classify_exception(
        aiosmtplib.SMTPConnectResponseError(554, "5.7.1 blocked using zen.spamhaus.org")
    )
    assert (greet_554.outcome, greet_554.result) == (S.policy_block, R.blocked)
    helo = classify_exception(aiosmtplib.SMTPHeloError(550, "5.7.1 HELO rejected"), stage="ehlo")
    assert helo.outcome == S.policy_block and helo.detail.startswith("ehlo:")
    sender = classify_exception(
        aiosmtplib.SMTPSenderRefused(553, "5.1.8 Sender address rejected", "a@b.fr"), stage="mail"
    )
    assert (sender.outcome, sender.result) == (S.policy_block, R.blocked)
    mail_4xx = classify_exception(
        aiosmtplib.SMTPSenderRefused(451, "4.3.0 try later", "a@b.fr"), stage="mail"
    )
    assert mail_4xx.outcome == S.temporary
    read_timeout = classify_exception(TimeoutError(), stage="rcpt")
    assert (read_timeout.outcome, read_timeout.result) == (S.infra_failure, R.timeout)


def test_random_probe_addresses_look_like_people_not_tests():
    shape = re.compile(r"^[a-z]+([._-][a-z]+)?@acme\.fr$")
    rng = Random(7)
    seen: set[str] = set()
    for _ in range(300):
        for addr in random_probe_addresses("acme.fr", 2, rng=rng):
            local = addr.split("@")[0]
            assert shape.match(addr), addr
            assert not re.search(r"test|catch|scout|probe|spam|abuse|admin|info|random|fake", local), addr
            assert 6 <= len(local) <= 20
            seen.add(addr)
    assert len(seen) == 600  # practically never collides
    a, b = random_probe_addresses("acme.fr", 2, rng=Random(1))
    assert "." in a.split("@")[0] and "." not in b.split("@")[0]  # first.last, then flast
    assert random_probe_addresses("acme.fr", 2, rng=Random(3)) == random_probe_addresses(
        "acme.fr", 2, rng=Random(3)
    )
    assert "." in random_local_part("first.last")


def test_uninformative_providers_and_hosts():
    assert uninformative_accepts(MailProvider.secure_gateway, [])
    assert uninformative_accepts(None, ["mta5.am0.yahoodns.net"])
    assert uninformative_accepts(MailProvider.unknown, ["eu-smtp-inbound-1.mimecast.com."])
    assert uninformative_accepts(None, ["mx0a-001.pphosted.com"])
    assert not uninformative_accepts(MailProvider.google_workspace, ["aspmx.l.google.com"])
    assert not uninformative_accepts(None, ["mx1.acme.fr"])


def test_provider_from_mx():
    assert (
        provider_from_mx(["aspmx.l.google.com", "alt1.aspmx.l.google.com"]) == MailProvider.google_workspace
    )
    assert provider_from_mx(["acme-fr.mail.protection.outlook.com"]) == MailProvider.microsoft_365
    assert provider_from_mx(["eu-smtp-inbound-1.mimecast.com"]) == MailProvider.secure_gateway
    assert provider_from_mx([]) == MailProvider.none


def test_wording_rules_ignore_echoed_addresses():
    # a domain that happens to contain a keyword ("greylist", "try-again", "spam") must not change the verdict
    for domain in ("greylist-agency.fr", "try-again-later.io", "spamhaus-consulting.com"):
        reply = f"5.1.1 <julie@{domain}>: Recipient address rejected: User unknown in virtual mailbox table"
        assert classify_rcpt(550, reply) == R.rejected
        assert not is_greylisting(f"5.1.1 <julie@{domain}>: User unknown")
    greylisted = "4.2.0 <x@g.example>: Recipient address rejected: Greylisted, see http://postgrey.example/help/g.example"
    assert classify_rcpt(450, greylisted) == R.temporary
    assert is_greylisting(greylisted)
