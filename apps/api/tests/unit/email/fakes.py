"""Test doubles for the email module: no real DNS, SMTP or HTTP."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiosmtplib

from scout.db.enums import EmailDiscoveryMethod, SmtpResult
from scout.email.dns import MxInfo
from scout.email.types import EmailCandidate, VerificationResult
from scout.email.verifier.fixture import FixtureVerifier

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "email" / "manifest.json"


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text())


def vr(
    address: str = "marie.dupont@agence-x.fr",
    *,
    syntax: bool = True,
    mx: bool | None = True,
    smtp: SmtpResult = SmtpResult.not_attempted,
    catch_all: bool | None = None,
    disposable: bool = False,
    role: bool = False,
    free: bool = False,
) -> VerificationResult:
    return VerificationResult(
        address=address,
        syntax_valid=syntax,
        mx_valid=mx,
        smtp_result=smtp,
        catch_all=catch_all,
        disposable=disposable,
        role_address=role,
        free_provider=free,
        verifier="fake",
    )


def cand(
    address: str = "marie.dupont@agence-x.fr",
    *,
    method: EmailDiscoveryMethod = EmailDiscoveryMethod.permutation,
    pattern: str | None = "{first}.{last}",
    pc: float = 0.4,
    samples: int = 0,
) -> EmailCandidate:
    return EmailCandidate(
        address=address, method=method, pattern=pattern, pattern_confidence=pc, supporting_samples=samples
    )


class FakeMxLookup:
    """Callable MX lookup returning canned MxInfo per domain (default: two MX hosts)."""

    def __init__(self, infos: dict[str, MxInfo] | None = None) -> None:
        self.infos = infos or {}
        self.calls: list[str] = []

    async def __call__(self, domain: str) -> MxInfo:
        self.calls.append(domain)
        return self.infos.get(domain) or MxInfo(
            domain, has_mx=True, mx_hosts=[f"mx1.{domain}", f"mx2.{domain}"]
        )


class FakeSMTP:
    """Minimal aiosmtplib.SMTP stand-in driven by a script."""

    def __init__(self, server: FakeSmtpServer, **kwargs: Any) -> None:
        self.server = server
        self.kwargs = kwargs
        self.is_connected = False
        self.rcpts: list[str] = []
        self.quit_called = False
        self.ehlo_called = False
        self.mail_from: str | None = None

    async def connect(self) -> None:
        exc = self.server.connect_errors.get(self.kwargs["hostname"])
        if exc is not None:
            raise exc
        self.is_connected = True

    async def ehlo(self) -> aiosmtplib.SMTPResponse:
        self.ehlo_called = True
        return aiosmtplib.SMTPResponse(250, "hello")

    async def mail(self, sender: str) -> aiosmtplib.SMTPResponse:
        self.mail_from = sender
        if self.server.mail_reply is not None:
            code, msg = self.server.mail_reply
            raise aiosmtplib.SMTPSenderRefused(code, msg, sender)
        return aiosmtplib.SMTPResponse(250, "ok")

    async def rcpt(self, recipient: str) -> aiosmtplib.SMTPResponse:
        self.rcpts.append(recipient)
        code, msg = self.server.rcpt_reply(recipient)
        if code not in (250, 251):
            raise aiosmtplib.SMTPRecipientRefused(code, msg, recipient)
        return aiosmtplib.SMTPResponse(code, msg)

    async def data(self, *_: Any, **__: Any) -> None:
        raise AssertionError("DATA must never be sent")

    async def quit(self) -> None:
        self.quit_called = True
        self.is_connected = False

    def close(self) -> None:
        self.is_connected = False


class FakeSmtpServer:
    """Factory for FakeSMTP clients; `mailboxes` accept, everything else gets `unknown_reply`."""

    def __init__(
        self,
        *,
        mailboxes: set[str] | None = None,
        catch_all: bool = False,
        unknown_reply: tuple[int, str] = (550, "5.1.1 <x>: Recipient address rejected: User unknown"),
        reply: Callable[[str], tuple[int, str]] | None = None,
        connect_errors: dict[str, BaseException] | None = None,
        mail_reply: tuple[int, str] | None = None,
    ) -> None:
        self.mailboxes = mailboxes or set()
        self.catch_all = catch_all
        self.unknown_reply = unknown_reply
        self._reply = reply
        self.connect_errors = connect_errors or {}
        self.mail_reply = mail_reply
        self.clients: list[FakeSMTP] = []

    def rcpt_reply(self, recipient: str) -> tuple[int, str]:
        if self._reply is not None:
            return self._reply(recipient)
        if self.catch_all or recipient in self.mailboxes:
            return 250, "2.1.5 Ok"
        return self.unknown_reply

    def __call__(self, **kwargs: Any) -> FakeSMTP:
        client = FakeSMTP(self, **kwargs)
        self.clients.append(client)
        return client

    @property
    def all_rcpts(self) -> list[str]:
        return [r for c in self.clients for r in c.rcpts]


class CountingVerifier:
    """Wraps a verifier and records every verified address."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = inner.name
        self.calls: list[str] = []

    async def verify(self, address: str) -> VerificationResult:
        self.calls.append(address)
        return await self.inner.verify(address)

    async def is_catch_all(self, domain: str) -> bool | None:
        return await self.inner.is_catch_all(domain)


def fixture_verifier(extra_domains: dict[str, Any] | None = None) -> CountingVerifier:
    manifest = load_manifest()
    manifest["email"]["domains"].update(extra_domains or {})
    return CountingVerifier(FixtureVerifier(manifest=manifest))
