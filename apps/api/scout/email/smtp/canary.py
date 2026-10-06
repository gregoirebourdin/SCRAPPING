"""Optional canary probes: detect early that OUR SMTP path got blocked (config-driven, cheap).

A canary is ONE random catch-all probe against a domain the profile table already knows to be NOT
catch-all on a major provider (Google Workspace / Microsoft 365). Expected answer: "user unknown".
Anything infrastructural (timeout, refused, policy block) is recorded by the prober in the health
monitor like any other session, so a blocked egress shows up before real requests pay for it.

When: half-open (cooldown expired, this caller holds the canary slot), health ``UNKNOWN`` (no recent
data — at most once per 5 minutes), or every ~200 RCPT probes. Enabled by ``settings.smtp_canary_enabled``
(read with ``getattr``: off unless the setting exists and is true).
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import sqlalchemy as sa
import structlog

from scout.config import get_settings
from scout.db.enums import MailProvider, SmtpHealthState
from scout.email.smtp.health import HealthGate, SmtpHealthMonitor

log = structlog.get_logger(__name__)

CANARY_EVERY_PROBES = 200
CANARY_MIN_INTERVAL_S = 300.0
CANARY_PROVIDERS = (MailProvider.google_workspace, MailProvider.microsoft_365)


def canary_enabled() -> bool:
    return bool(getattr(get_settings(), "smtp_canary_enabled", False))


def canary_due(gate: HealthGate, monitor: SmtpHealthMonitor) -> bool:
    if gate.half_open:
        return True
    if (
        monitor.last_canary_at is not None
        and time.monotonic() - monitor.last_canary_at < CANARY_MIN_INTERVAL_S
    ):
        return False
    return gate.state == SmtpHealthState.UNKNOWN or monitor.probes_since_canary >= CANARY_EVERY_PROBES


async def pick_canary_domain(
    *, provider: MailProvider | None = None, exclude: str | None = None
) -> tuple[str, list[str], MailProvider] | None:
    """A known non-catch-all domain on a major provider (same provider first when given)."""
    from scout.db.engine import session_scope
    from scout.db.models import DomainProfile

    providers = [provider] if provider in CANARY_PROVIDERS else list(CANARY_PROVIDERS)
    async with session_scope() as s:
        rows = (
            await s.execute(
                sa.select(DomainProfile.domain, DomainProfile.mx_hosts, DomainProfile.provider)
                .where(
                    DomainProfile.catch_all.is_(False),
                    DomainProfile.provider.in_(providers),
                    DomainProfile.accepts_mail.isnot(False),
                    sa.func.jsonb_array_length(DomainProfile.mx_hosts) > 0,
                    *([DomainProfile.domain != exclude] if exclude else []),
                )
                .order_by(DomainProfile.catch_all_checked_at.desc().nullslast())
                .limit(10)
            )
        ).all()
    if not rows:
        return None
    row = secrets.choice(rows)
    return row.domain, [str(h) for h in row.mx_hosts or []], MailProvider(row.provider)


async def maybe_run_canary(
    verifier: Any,
    gate: HealthGate,
    *,
    monitor: SmtpHealthMonitor,
    provider: MailProvider | None = None,
    exclude_domain: str | None = None,
    force: bool = False,
) -> HealthGate | None:
    """Run a canary when due; returns the refreshed gate (None when no canary ran)."""
    if not (force or canary_enabled()) or not getattr(verifier, "enabled", False) or not gate.may_probe:
        return None
    if not canary_due(gate, monitor):
        return None
    try:
        target = await pick_canary_domain(provider=provider, exclude=exclude_domain)
    except Exception as exc:
        log.warning("email.smtp.canary_pick_failed", error=str(exc))
        return None
    if target is None:
        return None
    domain, mx_hosts, canary_provider = target
    monitor.last_canary_at = time.monotonic()
    monitor.probes_since_canary = 0
    report = await verifier.probe_domain(
        domain, mx_hosts, [], check_catch_all=True, random_probes=1, provider=canary_provider
    )
    log.info("email.smtp.canary", domain=domain, session=report.session, catch_all=report.catch_all)
    return await monitor.gate(provider, claim_canary=False, enabled=True)
