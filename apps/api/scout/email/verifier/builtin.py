"""Built-in verifier: syntax → list flags → MX (cached) → optional SMTP RCPT probe.

The SMTP probe (only when `settings.smtp_enabled`) opens one session to the best MX host on
port 25: EHLO → MAIL FROM → RCPT TO <target> [→ RCPT TO <random, catch-all probe>] → QUIT.
DATA is never sent. Any connection problem degrades to `blocked`/`timeout`, never to an error.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import aiosmtplib
import structlog

from scout.config import get_settings
from scout.db.enums import SmtpResult, UsageCategory
from scout.email import dns
from scout.email.lists import is_disposable_domain, is_free_provider, is_role_local_part
from scout.email.syntax import is_valid_syntax, normalize_address, normalize_domain, split_address
from scout.email.types import VerificationResult
from scout.services.usage import record_usage
from scout.util.pools import pool

log = structlog.get_logger(__name__)

SMTP_PORT = 25
MAX_MX_HOSTS = 2
MEMORY_TTL_S = 3600.0

_ACCEPT_CODES = {250, 251}
_REJECT_CODES = {550, 551, 553, 554}
_USER_UNKNOWN_HINTS = (
    "5.1.1", "5.1.0", "5.1.10", "user unknown", "unknown user", "no such user", "does not exist",
    "doesn't exist", "mailbox unavailable", "mailbox not found", "recipient rejected", "address rejected",
    "invalid recipient", "recipient invalid", "no mailbox", "undeliverable", "not found", "unknown recipient",
)
_BLOCK_HINTS = (
    "5.7.", "spamhaus", "blocked", "blacklist", "blocklist", "block list", "banned", "denied", "reputation",
    "rbl", "dnsbl", "policy", "spam",
)

MxLookup = Callable[[str], Awaitable[dns.MxInfo]]
SmtpFactory = Callable[..., Any]


@dataclass
class RcptOutcome:
    address: str
    result: SmtpResult
    code: int | None = None
    message: str = ""


@dataclass
class ProbeOutcome:
    host: str | None
    rcpts: list[RcptOutcome] = field(default_factory=list)
    session_result: SmtpResult | None = None  # set when the session itself failed
    error: str | None = None


def classify_rcpt(code: int, message: str) -> SmtpResult:
    """Map an RCPT reply to accepted / rejected / blocked / unknown."""
    if code in _ACCEPT_CODES:
        return SmtpResult.accepted
    if 400 <= code < 500:
        return SmtpResult.unknown  # greylisting, rate limits, temporary failures
    if 500 <= code < 600:
        msg = message.lower()
        if any(h in msg for h in _USER_UNKNOWN_HINTS):
            return SmtpResult.rejected
        if any(h in msg for h in _BLOCK_HINTS):
            return SmtpResult.blocked  # our IP / HELO / sender refused, not the mailbox
        if code in _REJECT_CODES:
            return SmtpResult.rejected
    return SmtpResult.unknown


def _session_failure(exc: BaseException) -> tuple[SmtpResult, str]:
    """Classify a session-level failure (connect, greeting, EHLO, MAIL FROM)."""
    if isinstance(exc, aiosmtplib.SMTPTimeoutError | TimeoutError):
        return SmtpResult.timeout, f"timeout: {exc}"
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        result = SmtpResult.blocked if 500 <= exc.code < 600 else SmtpResult.unknown
        return result, f"{exc.code} {exc.message}"
    if isinstance(exc, aiosmtplib.SMTPServerDisconnected):
        return SmtpResult.unknown, f"disconnected: {exc}"
    if isinstance(exc, aiosmtplib.SMTPConnectError | OSError):
        return SmtpResult.blocked, f"connect failed: {exc}"
    return SmtpResult.unknown, f"{type(exc).__name__}: {exc}"


def random_probe_address(domain: str) -> str:
    """An improbable mailbox used to detect catch-all domains."""
    return f"scout-zz-{secrets.token_hex(8)}@{domain}"


def _catch_all_from(outcome: RcptOutcome) -> bool | None:
    if outcome.result == SmtpResult.accepted:
        return True
    if outcome.result == SmtpResult.rejected:
        return False
    return None


class BuiltinVerifier:
    """Deterministic verifier running inside the API process (no external service)."""

    name = "builtin"

    def __init__(
        self,
        *,
        smtp_enabled: bool | None = None,
        helo_domain: str | None = None,
        mail_from: str | None = None,
        timeout: float | None = None,
        mx_lookup: MxLookup | None = None,
        smtp_factory: SmtpFactory | None = None,
        use_db_cache: bool = True,
    ) -> None:
        s = get_settings()
        self.smtp_enabled = s.smtp_enabled if smtp_enabled is None else smtp_enabled
        self.helo_domain = helo_domain or s.smtp_helo_domain
        self.mail_from = mail_from or s.smtp_from_address
        self.timeout = timeout if timeout is not None else s.smtp_timeout
        self.use_db_cache = use_db_cache
        self._mx_lookup: MxLookup = mx_lookup or (lambda d: dns.mx_lookup(d, use_cache=use_db_cache))
        self._smtp_factory: SmtpFactory = smtp_factory or aiosmtplib.SMTP
        self._catch_all_memory: dict[str, tuple[float, bool]] = {}

    # ---- public API ---------------------------------------------------------------------------

    async def verify(self, address: str) -> VerificationResult:
        t0 = time.monotonic()
        addr = normalize_address(address)
        if addr is None or not is_valid_syntax(addr):
            local, _, domain = (address or "").strip().lower().rpartition("@")
            return VerificationResult(
                address=addr or (address or "").strip().lower(),
                syntax_valid=False,
                mx_valid=None,
                smtp_result=SmtpResult.not_attempted,
                catch_all=None,
                disposable=bool(domain) and is_disposable_domain(domain),
                role_address=bool(local) and is_role_local_part(local),
                free_provider=bool(domain) and is_free_provider(domain),
                verifier=self.name,
                raw={"syntax": "invalid"},
                error="invalid_syntax",
                duration_ms=self._ms(t0),
            )
        local, domain = split_address(addr)
        res = VerificationResult(
            address=addr,
            syntax_valid=True,
            mx_valid=None,
            smtp_result=SmtpResult.not_attempted,
            catch_all=None,
            disposable=is_disposable_domain(domain),
            role_address=is_role_local_part(local),
            free_provider=is_free_provider(domain),
            verifier=self.name,
            raw={"smtp_enabled": self.smtp_enabled},
        )
        if res.disposable:
            res.duration_ms = self._ms(t0)
            return res

        await self._record_usage()
        mx = await self._mx_lookup(domain)
        res.raw["mx"] = {
            "has_mx": mx.has_mx, "mx_hosts": mx.mx_hosts, "has_a": mx.has_a, "null_mx": mx.null_mx,
            "cached": mx.cached, "error": mx.error,
        }
        if mx.transient:
            res.error = mx.error
        elif not mx.accepts_mail:
            res.mx_valid = False
        else:
            res.mx_valid = True
            if self.smtp_enabled:
                await self._smtp_verify(res, domain, mx.mx_hosts or [domain])
        res.duration_ms = self._ms(t0)
        return res

    async def is_catch_all(self, domain: str) -> bool | None:
        if not self.smtp_enabled:
            return None
        d = normalize_domain(domain)
        if d is None:
            return None
        known = await self._known_catch_all(d)
        if known is not None:
            return known
        mx = await self._mx_lookup(d)
        if not mx.accepts_mail:
            return None
        async with pool("smtp"):
            probe = await self._probe(mx.mx_hosts or [d], [random_probe_address(d)])
        if probe.session_result is not None or not probe.rcpts:
            return None
        value = _catch_all_from(probe.rcpts[0])
        await self._remember_catch_all(d, value)
        return value

    # ---- SMTP ---------------------------------------------------------------------------------

    async def _smtp_verify(self, res: VerificationResult, domain: str, hosts: list[str]) -> None:
        known = await self._known_catch_all(domain)
        rcpts = [res.address] if known is not None else [res.address, random_probe_address(domain)]
        async with pool("smtp"):
            probe = await self._probe(hosts, rcpts)
        res.raw["smtp"] = {
            "host": probe.host,
            "rcpt": [{"address": r.address, "code": r.code, "message": r.message[:300], "result": r.result}
                     for r in probe.rcpts],
            "error": probe.error,
        }
        res.catch_all = known
        if probe.session_result is not None or not probe.rcpts:
            res.smtp_result = probe.session_result or SmtpResult.unknown
            res.error = probe.error
            return
        res.smtp_result = probe.rcpts[0].result
        if known is None and len(probe.rcpts) > 1:
            res.catch_all = _catch_all_from(probe.rcpts[1])
            res.raw["catch_all_source"] = "probe"
            await self._remember_catch_all(domain, res.catch_all)
        elif known is not None:
            res.raw["catch_all_source"] = "cache"

    async def _probe(self, hosts: list[str], recipients: list[str]) -> ProbeOutcome:
        """Try the best MX hosts in order; move on only when a host cannot be reached."""
        outcome = ProbeOutcome(host=None, session_result=SmtpResult.unknown, error="no MX host")
        for host in hosts[:MAX_MX_HOSTS]:
            outcome = await self._probe_host(host, recipients)
            if outcome.session_result not in (SmtpResult.blocked, SmtpResult.timeout):
                break
        return outcome

    async def _probe_host(self, host: str, recipients: list[str]) -> ProbeOutcome:
        client = self._smtp_factory(
            hostname=host,
            port=SMTP_PORT,
            timeout=self.timeout,
            local_hostname=self.helo_domain,
            use_tls=False,
            start_tls=False,
            validate_certs=False,
        )
        outcome = ProbeOutcome(host=host)
        budget = self.timeout * (3 + len(recipients))
        try:
            async with asyncio.timeout(budget):
                await client.connect()
                await client.ehlo()
                await client.mail(self.mail_from)
                for rcpt in recipients:
                    try:
                        resp = await client.rcpt(rcpt)
                        code, message = resp.code, resp.message
                    except aiosmtplib.SMTPRecipientRefused as exc:
                        code, message = exc.code, exc.message
                    outcome.rcpts.append(RcptOutcome(rcpt, classify_rcpt(code, message), code, message))
        except Exception as exc:
            if not outcome.rcpts:
                outcome.session_result, outcome.error = _session_failure(exc)
            else:  # the target was answered; a later failure only loses the catch-all probe
                outcome.error = _session_failure(exc)[1]
            log.info("email.smtp.session_failed", host=host, error=outcome.error)
        finally:
            with suppress(Exception):
                if getattr(client, "is_connected", False):
                    async with asyncio.timeout(self.timeout):
                        await client.quit()
            with suppress(Exception):
                client.close()
        return outcome

    # ---- catch-all memory -------------------------------------------------------------------

    async def _known_catch_all(self, domain: str) -> bool | None:
        hit = self._catch_all_memory.get(domain)
        if hit and time.monotonic() - hit[0] < MEMORY_TTL_S:
            return hit[1]
        if not self.use_db_cache:
            return None
        value = await dns.cached_catch_all(domain)
        if value is not None:
            self._catch_all_memory[domain] = (time.monotonic(), value)
        return value

    async def _remember_catch_all(self, domain: str, value: bool | None) -> None:
        if value is None:
            return
        self._catch_all_memory[domain] = (time.monotonic(), value)
        if self.use_db_cache:
            await dns.store_catch_all(domain, value)

    # ---- misc ---------------------------------------------------------------------------------

    @staticmethod
    def _ms(t0: float) -> int:
        return int((time.monotonic() - t0) * 1000)

    @staticmethod
    async def _record_usage() -> None:
        try:
            await record_usage(UsageCategory.verification_request, cost_usd=get_settings().cost_verification_usd)
        except Exception as exc:  # usage accounting must never break verification
            log.warning("email.usage_record_failed", error=str(exc))
