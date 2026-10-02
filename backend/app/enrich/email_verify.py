"""Free email verification: MX lookup always, SMTP ``RCPT TO`` when outbound port 25 is open.

Statuses
--------
``no_mx``      domain has no mail exchanger → the address cannot receive mail
``mx_valid``   mail exchanger exists (SMTP check unavailable or inconclusive)
``smtp_valid`` mailbox accepted by the MX
``catch_all``  the MX accepts any local part, so the mailbox cannot be proven
``invalid``    mailbox rejected (550)
"""

from __future__ import annotations

import asyncio
import logging
import random
import smtplib
import socket
import string
from dataclasses import dataclass

import dns.asyncresolver
import dns.exception

from ..config import settings

log = logging.getLogger(__name__)

GUESS_LOCALS = ["hello", "info", "contact", "team", "hi"]


@dataclass
class VerifyResult:
    email: str
    status: str
    mx: str | None = None
    detail: str = ""


class EmailVerifier:
    def __init__(self) -> None:
        self._mx_cache: dict[str, list[str]] = {}
        self._catch_all_cache: dict[str, bool] = {}
        self._mx_lock = asyncio.Lock()
        self._smtp_sem = asyncio.Semaphore(settings.smtp_concurrency)
        self.smtp_available: bool | None = None if settings.smtp_verify else False
        self._resolver = dns.asyncresolver.Resolver()
        self._resolver.lifetime = 6.0
        self._resolver.timeout = 3.0
        self.stats = {"mx_lookups": 0, "smtp_checks": 0, "smtp_timeouts": 0}
        self._smtp_failures = 0

    # --- MX ---------------------------------------------------------------------------------------------------
    async def mx_hosts(self, domain: str) -> list[str]:
        domain = domain.lower().strip()
        if domain in self._mx_cache:
            return self._mx_cache[domain]
        hosts: list[str] = []
        try:
            self.stats["mx_lookups"] += 1
            ans = await self._resolver.resolve(domain, "MX")
            hosts = [str(r.exchange).rstrip(".") for r in sorted(ans, key=lambda r: r.preference)]
            hosts = [h for h in hosts if h and h != "."]
        except (dns.exception.DNSException, Exception):
            # no MX: RFC 5321 falls back to the A record
            try:
                await self._resolver.resolve(domain, "A")
                hosts = [domain]
            except Exception:
                hosts = []
        self._mx_cache[domain] = hosts
        return hosts

    # --- SMTP -------------------------------------------------------------------------------------------------
    def _smtp_rcpt(self, mx: str, email: str) -> tuple[int, str]:
        with smtplib.SMTP(timeout=settings.smtp_timeout) as s:
            s.connect(mx, 25)
            s.ehlo_or_helo_if_needed()
            try:
                s.ehlo(settings.smtp_helo_domain)
            except smtplib.SMTPException:
                pass
            s.mail(settings.smtp_from_address)
            code, msg = s.rcpt(email)
            return code, msg.decode("utf-8", "ignore") if isinstance(msg, bytes) else str(msg)

    async def _smtp_check(self, mx: str, email: str) -> tuple[int | None, str]:
        async with self._smtp_sem:
            self.stats["smtp_checks"] += 1
            try:
                return await asyncio.wait_for(asyncio.to_thread(self._smtp_rcpt, mx, email), timeout=settings.smtp_timeout + 4)
            except (asyncio.TimeoutError, socket.timeout, TimeoutError, OSError):
                self.stats["smtp_timeouts"] += 1
                return None, "timeout"
            except smtplib.SMTPServerDisconnected:
                return None, "disconnected"
            except smtplib.SMTPException as e:
                return None, f"smtp:{e}"

    async def _probe_port25(self) -> bool:
        """Detect whether this network allows outbound SMTP at all (most ISPs/datacenters block it)."""
        if self.smtp_available is not None:
            return self.smtp_available
        async with self._mx_lock:
            if self.smtp_available is not None:
                return self.smtp_available
            for host in ("gmail-smtp-in.l.google.com", "alt1.gmail-smtp-in.l.google.com", "mx1.hotmail.com"):
                try:
                    _, w = await asyncio.wait_for(asyncio.open_connection(host, 25), timeout=6)
                    w.close()
                    self.smtp_available = True
                    log.info("outbound SMTP (port 25) is open: mailbox verification enabled")
                    return True
                except Exception:
                    continue
            self.smtp_available = False
            log.info("outbound SMTP (port 25) is blocked: falling back to MX-only verification")
            return False

    async def _is_catch_all(self, domain: str, mx: str) -> bool | None:
        if domain in self._catch_all_cache:
            return self._catch_all_cache[domain]
        rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=14))
        code, _ = await self._smtp_check(mx, f"zz{rnd}@{domain}")
        if code is None:
            return None
        res = 200 <= code < 300
        self._catch_all_cache[domain] = res
        return res

    # --- public ----------------------------------------------------------------------------------------------
    async def verify(self, email: str) -> VerifyResult:
        email = email.strip().lower()
        if "@" not in email:
            return VerifyResult(email, "invalid", detail="syntax")
        domain = email.rsplit("@", 1)[1]
        hosts = await self.mx_hosts(domain)
        if not hosts:
            return VerifyResult(email, "no_mx")
        mx = hosts[0]
        if not await self._probe_port25() or self._smtp_failures > 25:
            return VerifyResult(email, "mx_valid", mx=mx, detail="smtp_unavailable")
        code, msg = await self._smtp_check(mx, email)
        if code is None:
            self._smtp_failures += 1
            return VerifyResult(email, "mx_valid", mx=mx, detail=msg)
        if 200 <= code < 300:
            ca = await self._is_catch_all(domain, mx)
            if ca:
                return VerifyResult(email, "catch_all", mx=mx, detail=msg[:120])
            return VerifyResult(email, "smtp_valid", mx=mx, detail=msg[:120])
        if code == 550 or code == 551 or code == 553 or (code == 554 and "no such" in msg.lower()):
            return VerifyResult(email, "invalid", mx=mx, detail=msg[:120])
        return VerifyResult(email, "mx_valid", mx=mx, detail=f"{code} {msg[:100]}")

    async def guess(self, domain: str) -> list[VerifyResult]:
        """Propose generic mailboxes for a domain; keep the ones that verify (or all, as mx_valid, when SMTP is blocked)."""
        hosts = await self.mx_hosts(domain)
        if not hosts:
            return []
        out: list[VerifyResult] = []
        smtp = await self._probe_port25()
        for local in GUESS_LOCALS:
            email = f"{local}@{domain}"
            if smtp:
                r = await self.verify(email)
                if r.status in ("smtp_valid",):
                    out.append(r)
                elif r.status == "catch_all":
                    out.append(r)
                    break  # all guesses would "pass"; keep just the first
            else:
                out.append(VerifyResult(email, "mx_valid", mx=hosts[0], detail="guessed"))
        return out


_verifier: EmailVerifier | None = None


def get_verifier() -> EmailVerifier:
    global _verifier
    if _verifier is None:
        _verifier = EmailVerifier()
    return _verifier
