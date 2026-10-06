"""Built-in verifier: syntax → list flags → MX (cached) → optional SMTP RCPT probe.

The SMTP part is the batched prober of ``scout.email.smtp.session`` used with a single target:
EHLO (HELO fallback) → STARTTLS when offered → MAIL FROM → RCPT TO <target> [+ random catch-all
probes when the domain's catch-all state is unknown] → QUIT. DATA is never sent. Replies are classified
by ``scout.email.smtp.classify`` (RFC 3463 first): only an explicit user-unknown answer is ``rejected``;
4xx / greylisting → ``temporary``; policy / reputation / connection problems → ``blocked`` / ``timeout``.

The SMTP health gate is honoured: while our SMTP path is BLOCKED (or the HELO identity is refused)
no probe is made and ``smtp_result`` is ``not_attempted`` — never ``rejected``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from scout.config import get_settings
from scout.db.enums import SmtpResult, UsageCategory
from scout.email import dns
from scout.email.lists import is_disposable_domain, is_free_provider, is_role_local_part
from scout.email.smtp.classify import classify_rcpt, provider_from_mx, random_probe_addresses
from scout.email.smtp.health import MemoryHealthStore, SmtpHealthMonitor, get_monitor
from scout.email.smtp.session import SMTP_PORT, ProbeReport, SmtpProber
from scout.email.syntax import is_valid_syntax, normalize_address, normalize_domain, split_address
from scout.email.types import VerificationResult
from scout.services.usage import record_usage

log = structlog.get_logger(__name__)

__all__ = ["MAX_MX_HOSTS", "SMTP_PORT", "BuiltinVerifier", "classify_rcpt", "random_probe_address"]

MAX_MX_HOSTS = 3
MEMORY_TTL_S = 3600.0

MxLookup = Callable[[str], Awaitable[dns.MxInfo]]
SmtpFactory = Callable[..., Any]


def random_probe_address(domain: str) -> str:
    """An improbable but plausible-looking mailbox used to detect catch-all domains."""
    return random_probe_addresses(domain, 1)[0]


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
        health_monitor: SmtpHealthMonitor | None = None,
        port: int = SMTP_PORT,
    ) -> None:
        s = get_settings()
        self.smtp_enabled = s.smtp_enabled if smtp_enabled is None else smtp_enabled
        self.use_db_cache = use_db_cache
        self._mx_lookup: MxLookup = mx_lookup or (lambda d: dns.mx_lookup(d, use_cache=use_db_cache))
        # Without the database (tests, scripts) the health gate still works, in memory.
        self.monitor = health_monitor or (
            get_monitor()
            if use_db_cache
            else SmtpHealthMonitor(MemoryHealthStore(), enabled=lambda: self.smtp_enabled)
        )
        self.prober = SmtpProber(
            enabled=self.smtp_enabled,
            helo_domain=helo_domain,
            mail_from=mail_from,
            timeout=timeout,
            port=port,
            smtp_factory=smtp_factory,
            monitor=self.monitor,
            max_mx_hosts=MAX_MX_HOSTS,
            mx_lookup=self._mx_lookup,
        )
        self.helo_domain = self.prober.helo_domain
        self.mail_from = self.prober.mail_from
        self.timeout = self.prober.timeout
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
            "has_mx": mx.has_mx,
            "mx_hosts": mx.mx_hosts,
            "has_a": mx.has_a,
            "null_mx": mx.null_mx,
            "cached": mx.cached,
            "error": mx.error,
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
        hosts = mx.mx_hosts or [d]
        provider = provider_from_mx(mx.mx_hosts, domain=d) if mx.mx_hosts else None
        gate = await self.monitor.gate(provider, claim_canary=True, enabled=self.prober.enabled)
        if not gate.may_probe:
            return None
        report = await self.prober.probe_domain(d, hosts, [], check_catch_all=True, provider=provider)
        await self._remember_catch_all(d, report.catch_all)
        return report.catch_all

    # ---- SMTP ---------------------------------------------------------------------------------

    async def _smtp_verify(self, res: VerificationResult, domain: str, hosts: list[str]) -> None:
        provider = provider_from_mx(hosts, domain=domain) if hosts != [domain] else None
        gate = await self.monitor.gate(provider, claim_canary=True, enabled=self.prober.enabled)
        if not gate.may_probe:
            res.smtp_result = SmtpResult.not_attempted
            res.raw["smtp"] = {"health": gate.state.value, "reason": gate.reason}
            res.error = (
                f"SMTP health gate closed ({gate.state.value})"
                if self.prober.enabled
                else (self.prober.disabled_reason() or "SMTP probing disabled")
            )
            return
        known = await self._known_catch_all(domain)
        report = await self.prober.probe_domain(
            domain, hosts, [res.address], check_catch_all=known is None, provider=provider
        )
        res.raw["smtp"] = self._raw(report)
        verdict = report.verdicts.get(res.address)
        res.smtp_result = verdict.result if verdict is not None else SmtpResult.unknown
        if verdict is None or verdict.code is None:
            res.error = report.error
        res.catch_all = known
        if known is not None:
            res.raw["catch_all_source"] = "cache"
        elif report.catch_all is not None:
            res.catch_all = report.catch_all
            res.raw["catch_all_source"] = "probe"
            await self._remember_catch_all(domain, report.catch_all)

    @staticmethod
    def _raw(report: ProbeReport) -> dict[str, Any]:
        rcpts = [*report.verdicts.values(), *report.random_verdicts.values()]
        return {
            "host": report.mx_host,
            "session": report.session.value,
            "rcpt": [
                {"address": v.address, "code": v.code, "message": (v.message or "")[:300], "result": v.result}
                for v in rcpts
            ],
            "catch_all": report.catch_all,
            "catch_all_confidence": report.catch_all_confidence,
            "tls": report.tls,
            "hosts_tried": report.hosts_tried,
            "notes": report.notes,
            "error": report.error,
        }

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
            await record_usage(
                UsageCategory.verification_request, cost_usd=get_settings().cost_verification_usd
            )
        except Exception as exc:  # usage accounting must never break verification
            log.warning("email.usage_record_failed", error=str(exc))
