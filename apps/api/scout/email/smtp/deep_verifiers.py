"""``DeepVerifier``: one batched probe per domain, whatever does the SMTP talking.

* ``BuiltinDeepVerifier`` — our own port-25 prober (``session.SmtpProber``).
* ``ServiceDeepVerifier`` — the Go/AfterShip service (``scout.email.verifier.service.ServiceVerifier``),
  called per address and folded into one ``DomainProbeResult`` (catch-all from the service's
  ``/v1/catch-all`` endpoint); its error texts are re-read by our classifier so 4xx/policy answers are
  never mistaken for anything final. While the service is skipped (down, or its port 25 blocked — see the
  service's breaker) whole domains go to the ``fallback`` deep verifier (our own batched prober).
* ``WorldDeepVerifier`` — the deterministic simulated mail world (``world.py``).

``get_deep_verifier()`` follows ``settings.verifier_backend`` like ``scout.email.verifier.build_verifier``:
``fixture`` → world verifier (default world from the fixture manifest, or ``set_default_world``),
``service`` (or ``auto`` with a service URL) → service, otherwise builtin.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Protocol, runtime_checkable

import structlog

from scout.config import Settings, get_settings
from scout.db.enums import MailProvider, SmtpResult
from scout.email.contracts import DomainProbeResult, RcptVerdict, SessionOutcome
from scout.email.smtp.classify import classify_rcpt_reply, provider_from_mx, uninformative_accepts
from scout.email.smtp.health import SmtpHealthMonitor, get_monitor
from scout.email.smtp.session import (
    DEFAULT_RANDOM_PROBES,
    UNINFORMATIVE_CATCH_ALL_CONFIDENCE,
    ProbeReport,
    SmtpProber,
)
from scout.email.smtp.world import MailWorld, WorldDeepVerifier
from scout.email.syntax import normalize_address, normalize_domain, split_address
from scout.email.types import VerificationResult
from scout.email.verifier.service import ServiceVerifier, shared_breaker

log = structlog.get_logger(__name__)

__all__ = [
    "BuiltinDeepVerifier",
    "DeepVerifier",
    "ServiceDeepVerifier",
    "WorldDeepVerifier",
    "build_deep_verifier",
    "get_deep_verifier",
    "get_default_world",
    "set_deep_verifier",
    "set_default_world",
]


@runtime_checkable
class DeepVerifier(Protocol):
    name: str

    @property
    def enabled(self) -> bool:
        """False when this verifier will not talk SMTP at all (disabled, refused identity…)."""
        ...

    async def probe_domain(
        self,
        domain: str,
        mx_hosts: list[str],
        addresses: list[str],
        *,
        check_catch_all: bool,
        random_probes: int = DEFAULT_RANDOM_PROBES,
        provider: MailProvider | None = None,
        catch_all_addresses: list[str] | None = None,
    ) -> DomainProbeResult:
        """One batched probe of every address of ``domain`` (+ random catch-all probes)."""
        ...


class BuiltinDeepVerifier(SmtpProber):
    """Our own RCPT prober over port 25 (see ``scout.email.smtp.session``)."""

    name = "builtin"


# --------------------------------------------------------------------------------------------
# Go / AfterShip service
# --------------------------------------------------------------------------------------------

_CODE = re.compile(r"\b([245]\d\d)\b")


def _refine(res: VerificationResult) -> tuple[SmtpResult, int | None, str]:
    """AfterShip's mapping, re-checked with our classifier for temporary / policy answers."""
    smtp = res.raw.get("smtp") if isinstance(res.raw, dict) else None
    err = str((smtp or {}).get("error") or res.error or "")
    if err and res.smtp_result in (SmtpResult.unknown, SmtpResult.blocked, SmtpResult.rejected):
        m = _CODE.search(err)
        if m:
            cls = classify_rcpt_reply(int(m.group(1)), err)
            if cls.result in (SmtpResult.temporary, SmtpResult.blocked):
                return cls.result, cls.code, err[:500]
    return res.smtp_result, None, err[:500]


class ServiceDeepVerifier:
    name = "aftership"
    enabled = True

    def __init__(
        self,
        service: ServiceVerifier,
        *,
        concurrency: int = 4,
        monitor: SmtpHealthMonitor | None = None,
        record_health: bool = True,
        fallback: DeepVerifier | None = None,
    ) -> None:
        self.service = service
        self.fallback = fallback
        self.concurrency = max(1, concurrency)
        self._monitor = monitor
        self.record_health = record_health

    async def probe_domain(
        self,
        domain: str,
        mx_hosts: list[str],
        addresses: list[str],
        *,
        check_catch_all: bool,
        random_probes: int = DEFAULT_RANDOM_PROBES,
        provider: MailProvider | None = None,
        catch_all_addresses: list[str] | None = None,
    ) -> DomainProbeResult:
        if self.fallback is not None and self.fallback.enabled and self.service.breaker.open():
            return await self.fallback.probe_domain(
                domain,
                mx_hosts,
                addresses,
                check_catch_all=check_catch_all,
                random_probes=random_probes,
                provider=provider,
                catch_all_addresses=catch_all_addresses,
            )
        t0 = time.monotonic()
        d = normalize_domain(domain) or (domain or "").strip().lower()
        report = ProbeReport(domain=d, session=SessionOutcome.not_attempted, verifier=self.name)
        targets: list[str] = []
        for raw in addresses:
            addr = normalize_address(raw)
            if addr is None or split_address(addr)[1] != d:
                key = addr or (raw or "").strip().lower()
                report.verdicts[key] = RcptVerdict(
                    key, SmtpResult.not_attempted, None, "not an address of this domain"
                )
            elif addr not in targets:
                targets.append(addr)

        sem = asyncio.Semaphore(self.concurrency)

        async def one(a: str) -> VerificationResult:
            async with sem:
                return await self.service.verify(a)

        results = await asyncio.gather(*(one(a) for a in targets))
        fallback_used = any(r.verifier != self.service.name for r in results)
        for a, res in zip(targets, results, strict=True):
            result, code, message = _refine(res)
            report.verdicts[a] = RcptVerdict(a, result, code, message)
        report.probes = len(targets)

        catch_all: bool | None = None
        if check_catch_all or not targets:
            try:
                catch_all = await self.service.is_catch_all(d)
            except Exception as exc:  # never break the batch for the catch-all side question
                log.warning("email.smtp.service_catch_all_failed", domain=d, error=str(exc))
        if catch_all is None:
            catch_all = next((r.catch_all for r in results if r.catch_all is not None), None)
        report.catch_all = catch_all
        report.catch_all_confidence = 0.9 if catch_all is not None else None

        prov = (
            provider if provider is not None else (provider_from_mx(mx_hosts, domain=d) if mx_hosts else None)
        )
        report.provider = prov
        if uninformative_accepts(prov, mx_hosts) and report.catch_all is not False:
            report.uninformative_accepts = True
            report.catch_all, report.catch_all_confidence = None, UNINFORMATIVE_CATCH_ALL_CONFIDENCE
            report.notes.append("accept-then-bounce provider: RCPT acceptance does not prove a mailbox")
            for a in targets:
                v = report.verdicts[a]
                if v.result == SmtpResult.accepted:
                    report.verdicts[a] = RcptVerdict(
                        a, SmtpResult.unknown, v.code, f"uninformative (accept-then-bounce): {v.message}"
                    )

        report.session = self._session([report.verdicts[a] for a in targets], bool(targets))
        report.error = next((r.error for r in results if r.error), None)
        report.duration_ms = int((time.monotonic() - t0) * 1000)
        if self.record_health and not fallback_used and report.session != SessionOutcome.not_attempted:
            try:
                await (self._monitor or get_monitor()).record_session(
                    prov, report.session, domain=d, probes=report.probes
                )
            except Exception as exc:
                log.warning("email.smtp.health_record_failed", domain=d, error=str(exc))
        return report

    @staticmethod
    def _session(verdicts: list[RcptVerdict], had_targets: bool) -> SessionOutcome:
        rs = [v.result for v in verdicts]
        if not had_targets or not rs or all(r == SmtpResult.not_attempted for r in rs):
            return SessionOutcome.not_attempted
        if any(r in (SmtpResult.accepted, SmtpResult.rejected, SmtpResult.unknown) for r in rs):
            return SessionOutcome.ok
        if all(r == SmtpResult.temporary for r in rs):
            return SessionOutcome.temporary
        if all(r == SmtpResult.blocked for r in rs) and any(v.code for v in verdicts):
            return SessionOutcome.policy_block
        if any(r == SmtpResult.temporary for r in rs):
            return SessionOutcome.temporary
        return SessionOutcome.infra_failure


# --------------------------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------------------------

_verifier: DeepVerifier | None = None
_default_world: MailWorld | None = None


def get_default_world(settings: Settings | None = None) -> MailWorld:
    """World used by the ``fixture`` backend: ``set_default_world`` or the fixture manifest (else empty)."""
    global _default_world
    if _default_world is None:
        s = settings or get_settings()
        _default_world = (
            MailWorld.from_manifest_path(s.discovery_fixture_manifest)
            if s.discovery_fixture_manifest
            else MailWorld()
        )
    return _default_world


def set_default_world(world: MailWorld | None) -> None:
    """Tests / benchmark: the world behind the ``fixture`` backend (resets the cached verifier)."""
    global _default_world, _verifier
    _default_world = world
    _verifier = None


def build_deep_verifier(settings: Settings | None = None) -> DeepVerifier:
    s = settings or get_settings()
    backend = s.verifier_backend
    if backend == "auto":
        backend = "service" if s.verifier_service_url else "builtin"
    if backend == "fixture":
        if s.is_production:
            raise RuntimeError("the simulated mail world is a test backend and is forbidden in production")
        return WorldDeepVerifier(get_default_world(s), monitor=get_monitor())
    if backend == "service" and s.verifier_service_url:
        from scout.email.verifier.builtin import BuiltinVerifier

        token = s.verifier_service_token.get_secret_value() if s.verifier_service_token else None
        service = ServiceVerifier(
            s.verifier_service_url,
            token,
            fallback=BuiltinVerifier(),
            breaker=shared_breaker(s.verifier_service_url),
        )
        return ServiceDeepVerifier(service, fallback=BuiltinDeepVerifier())
    return BuiltinDeepVerifier()


def get_deep_verifier() -> DeepVerifier:
    """Process-wide deep verifier (cached)."""
    global _verifier
    if _verifier is None:
        _verifier = build_deep_verifier()
    return _verifier


def set_deep_verifier(verifier: DeepVerifier | None) -> None:
    """Override (tests) or reset (None → rebuilt from settings on next use)."""
    global _verifier
    _verifier = verifier
