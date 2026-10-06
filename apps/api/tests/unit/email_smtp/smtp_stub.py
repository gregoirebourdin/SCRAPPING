"""Test doubles for the SMTP layer: a tiny asyncio SMTP server on 127.0.0.1 and a scripted fake client.

No real network, no real mail server: the stub speaks just enough ESMTP for RCPT probing.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable
from typing import Any

import aiosmtplib

Reply = tuple[int, str]
_ADDR = re.compile(r"<([^>]*)>")


class StubSmtpServer:
    """Line-protocol SMTP stub. ``rcpt`` decides each RCPT reply; everything else is configurable."""

    def __init__(
        self,
        *,
        rcpt: Callable[[str], Reply] | None = None,
        mailboxes: set[str] | None = None,
        greeting: Reply = (220, "stub.mx ESMTP ready"),
        ehlo: Reply | None = None,
        helo: Reply = (250, "stub.mx"),
        mail: Reply = (250, "2.1.0 Ok"),
        starttls: bool = False,
        greeting_delay_s: float = 0.0,
    ) -> None:
        self.mailboxes = mailboxes or set()
        self._rcpt = rcpt
        self.greeting = greeting
        self.ehlo = ehlo
        self.helo = helo
        self.mail = mail
        self.starttls = starttls
        self.greeting_delay_s = greeting_delay_s
        self.connections = 0
        self.commands: list[str] = []
        self.sessions: list[list[str]] = []  # commands per connection
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    def rcpt_reply(self, address: str) -> Reply:
        if self._rcpt is not None:
            return self._rcpt(address)
        if address in self.mailboxes:
            return 250, "2.1.5 Ok"
        return 550, f"5.1.1 <{address}>: Recipient address rejected: User unknown in virtual mailbox table"

    async def __aenter__(self) -> StubSmtpServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        assert self._server is not None
        self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), timeout=2)

    @property
    def rcpts(self) -> list[str]:
        return [_ADDR.search(c).group(1) for c in self.commands if c.upper().startswith("RCPT")]  # type: ignore[union-attr]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        session: list[str] = []
        self.sessions.append(session)

        async def send(code: int, text: str, *, more: list[str] | None = None) -> None:
            lines = [*(more or []), text]
            payload = "".join(
                f"{code}{'-' if i < len(lines) - 1 else ' '}{ln}\r\n" for i, ln in enumerate(lines)
            )
            writer.write(payload.encode())
            await writer.drain()

        try:
            if self.greeting_delay_s:
                await asyncio.sleep(self.greeting_delay_s)
            await send(*self.greeting)
            if self.greeting[0] != 220:
                return
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                cmd = raw.decode(errors="replace").strip()
                self.commands.append(cmd)
                session.append(cmd)
                verb = cmd.split(" ", 1)[0].upper()
                if verb == "EHLO":
                    if self.ehlo is not None:
                        await send(*self.ehlo)
                    else:
                        ext = ["stub.mx", "PIPELINING", "SIZE 10240000"] + (
                            ["STARTTLS"] if self.starttls else []
                        )
                        await send(250, ext[-1], more=ext[:-1])
                elif verb == "HELO":
                    await send(*self.helo)
                elif verb == "STARTTLS":
                    await send(220, "2.0.0 Ready to start TLS")
                    return  # then hang up: the client's TLS handshake fails
                elif verb == "MAIL":
                    await send(*self.mail)
                elif verb == "RCPT":
                    m = _ADDR.search(cmd)
                    code, text = self.rcpt_reply(m.group(1).lower() if m else "")
                    await send(code, text)
                    if code == 421:
                        return
                elif verb in ("RSET", "NOOP"):
                    await send(250, "2.0.0 Ok")
                elif verb == "QUIT":
                    await send(221, "2.0.0 Bye")
                    return
                elif verb == "DATA":
                    await send(554, "5.5.1 DATA must never be sent by a prober")
                else:
                    await send(502, "5.5.2 Error: command not recognized")
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            with contextlib.suppress(Exception):
                writer.close()


class ScriptedSMTP:
    """aiosmtplib.SMTP stand-in (per-host behaviours) for MX walking / HELO / STARTTLS paths."""

    def __init__(self, factory: ScriptedFactory, **kwargs: Any) -> None:
        self.f = factory
        self.kwargs = kwargs
        self.host: str = kwargs["hostname"]
        self.is_connected = False
        self.rcpts: list[str] = []
        self.calls: list[str] = []

    async def connect(self) -> None:
        self.calls.append("connect")
        exc = self.f.connect_errors.get(self.host)
        if exc is not None:
            raise exc
        self.is_connected = True

    async def ehlo(self) -> aiosmtplib.SMTPResponse:
        self.calls.append("ehlo")
        if self.f.ehlo_reply is not None:
            raise aiosmtplib.SMTPHeloError(*self.f.ehlo_reply)
        return aiosmtplib.SMTPResponse(250, "ok")

    async def helo(self) -> aiosmtplib.SMTPResponse:
        self.calls.append("helo")
        return aiosmtplib.SMTPResponse(250, "ok")

    def supports_extension(self, ext: str) -> bool:
        return ext.lower() == "starttls" and self.f.starttls

    async def starttls(self, **_: Any) -> aiosmtplib.SMTPResponse:
        self.calls.append("starttls")
        if self.f.tls_error is not None:
            self.is_connected = False
            raise self.f.tls_error
        return aiosmtplib.SMTPResponse(220, "ready")

    async def mail(self, sender: str) -> aiosmtplib.SMTPResponse:
        self.calls.append("mail")
        if self.f.mail_reply is not None:
            raise aiosmtplib.SMTPSenderRefused(*self.f.mail_reply, sender)
        return aiosmtplib.SMTPResponse(250, "ok")

    async def rcpt(self, recipient: str) -> aiosmtplib.SMTPResponse:
        self.calls.append("rcpt")
        self.rcpts.append(recipient)
        if self.f.rcpt_errors:
            raise self.f.rcpt_errors.pop(0)
        code, msg = self.f.rcpt_reply(recipient)
        if code not in (250, 251):
            raise aiosmtplib.SMTPRecipientRefused(code, msg, recipient)
        return aiosmtplib.SMTPResponse(code, msg)

    async def data(self, *_: Any, **__: Any) -> None:
        raise AssertionError("DATA must never be sent")

    async def quit(self) -> None:
        self.calls.append("quit")
        self.is_connected = False

    def close(self) -> None:
        self.is_connected = False


class ScriptedFactory:
    def __init__(
        self,
        *,
        mailboxes: set[str] | None = None,
        catch_all: bool = False,
        reply: Callable[[str], Reply] | None = None,
        connect_errors: dict[str, BaseException] | None = None,
        ehlo_reply: Reply | None = None,
        mail_reply: Reply | None = None,
        starttls: bool = False,
        tls_error: BaseException | None = None,
        rcpt_errors: list[BaseException] | None = None,
    ) -> None:
        self.mailboxes = mailboxes or set()
        self.catch_all = catch_all
        self._reply = reply
        self.connect_errors = connect_errors or {}
        self.ehlo_reply = ehlo_reply
        self.mail_reply = mail_reply
        self.starttls = starttls
        self.tls_error = tls_error
        self.rcpt_errors = list(rcpt_errors or [])
        self.clients: list[ScriptedSMTP] = []

    def rcpt_reply(self, recipient: str) -> Reply:
        if self._reply is not None:
            return self._reply(recipient)
        if self.catch_all or recipient in self.mailboxes:
            return 250, "2.1.5 Ok"
        return 550, "5.1.1 user unknown"

    def __call__(self, **kwargs: Any) -> ScriptedSMTP:
        c = ScriptedSMTP(self, **kwargs)
        self.clients.append(c)
        return c

    @property
    def hosts(self) -> list[str]:
        return [c.host for c in self.clients]
