"""Deterministic simulated mail world — tests and the email benchmark (no network, no database).

A ``MailWorld`` maps domains to a ``DomainSpec`` (mailboxes, catch-all, provider, behaviour, latency)
plus global infrastructure flags (``port25_blocked``). ``WorldDeepVerifier`` runs the SAME batching,
MX-walking, catch-all and classification code as the real prober (``session.BatchProber``) against
simulated SMTP replies (real-world reply strings), so a benchmark measures the production logic.

Behaviours:

* ``normal``             — 250 for existing mailboxes (or everything when ``catch_all``), provider-style
                           user-unknown reply otherwise (Google 5.1.1, Microsoft 5.4.1 DBEB, Postfix 5.1.1).
* ``greylist_first``     — the first ``greylist_retries`` RCPTs of each address get 450 4.2.0 greylisted,
                           then ``normal`` (state kept across sessions, like postgrey's triplets).
* ``temporary_always``   — 421 4.7.0 at the greeting (session temporary).
* ``policy_block``       — 554 5.7.1 Spamhaus refusal at RCPT (our IP is listed).
* ``timeout``            — every MX host times out (dead MX).
* ``accept_all_gateway`` — a secure gateway that accepts every RCPT (and would bounce later).

Latency is simulated (accumulated, not slept) unless ``WorldDeepVerifier(sleep=True)``.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from typing import Any, Literal

from scout.db.enums import MailProvider, SmtpResult
from scout.email.contracts import SessionOutcome
from scout.email.dns import MxInfo
from scout.email.smtp.health import SmtpHealthMonitor
from scout.email.smtp.session import (
    DEFAULT_RANDOM_PROBES,
    BatchProber,
    ConnectionResult,
    MxLookup,
    ProbeReport,
)
from scout.email.syntax import normalize_address, normalize_domain, split_address

Behaviour = Literal[
    "normal", "greylist_first", "temporary_always", "policy_block", "timeout", "accept_all_gateway"
]

GOOGLE_UNKNOWN = (
    550,
    "5.1.1 The email account that you tried to reach does not exist. Please try double-checking the "
    "recipient's email address for typos or unnecessary spaces. For more information, go to "
    "https://support.google.com/mail/?p=NoSuchUser - gsmtp",
)
MICROSOFT_DBEB = (
    550,
    "5.4.1 Recipient address rejected: Access denied. For more information see https://aka.ms/EXOSmtpErrors "
    "[AM0PR02MB1234.eurprd02.prod.outlook.com]",
)
POSTFIX_UNKNOWN = (550, "5.1.1 <{addr}>: Recipient address rejected: User unknown in virtual mailbox table")
GREYLISTED = (
    450,
    "4.2.0 <{addr}>: Recipient address rejected: Greylisted, see http://postgrey.schweikert.ch/help/{domain}.html",
)
TRY_LATER = (421, "4.7.0 Try again later, closing connection.")
SPAMHAUS = (554, "5.7.1 Service unavailable; Client host [203.0.113.7] blocked using zen.spamhaus.org")
ACCEPTED = (250, "2.1.5 Ok")

_MX_BY_PROVIDER: dict[MailProvider, list[str]] = {
    MailProvider.google_workspace: ["aspmx.l.google.com", "alt1.aspmx.l.google.com"],
    MailProvider.microsoft_365: ["{tenant}.mail.protection.outlook.com"],
    MailProvider.secure_gateway: ["eu-smtp-inbound-1.mimecast.com", "eu-smtp-inbound-2.mimecast.com"],
    MailProvider.ovh: ["mx1.mail.ovh.net", "mx2.mail.ovh.net"],
}


@dataclass
class DomainSpec:
    mailboxes: set[str] = field(
        default_factory=set
    )  # full addresses (local parts are completed by MailWorld.add)
    catch_all: bool = False
    provider: MailProvider = MailProvider.unknown
    behaviour: Behaviour = "normal"
    latency_ms: int = 40
    greylist_retries: int = 1
    mx_hosts: list[str] = field(default_factory=list)
    accepts_mail: bool = True  # False: null MX


@dataclass
class MailWorld:
    domains: dict[str, DomainSpec] = field(default_factory=dict)
    port25_blocked: bool = False
    smtp_disabled: bool = False
    timeout_ms: int = 10_000
    seed: int = 0
    # ---- statistics (reset with reset_stats) ----
    sessions: int = 0  # probe_domain calls that reached the network layer
    connections: int = 0
    rcpt_commands: int = 0
    simulated_latency_ms: int = 0
    sessions_by_domain: Counter[str] = field(default_factory=Counter)
    rcpt_attempts: Counter[str] = field(default_factory=Counter)  # per address (greylisting memory)

    # ---- building ----------------------------------------------------------------------------
    def add(
        self,
        domain: str,
        *,
        mailboxes: set[str] | list[str] | tuple[str, ...] = (),
        catch_all: bool = False,
        provider: MailProvider = MailProvider.unknown,
        behaviour: Behaviour = "normal",
        latency_ms: int = 40,
        greylist_retries: int = 1,
        mx_hosts: list[str] | None = None,
        accepts_mail: bool = True,
    ) -> DomainSpec:
        d = normalize_domain(domain) or domain.lower()
        boxes: set[str] = set()
        for m in mailboxes:
            addr = normalize_address(m if "@" in m else f"{m}@{d}")
            if addr:
                boxes.add(addr)
        hosts = mx_hosts or [
            h.format(tenant=d.replace(".", "-"))
            for h in _MX_BY_PROVIDER.get(provider, [f"mx1.{d}", f"mx2.{d}"])
        ]
        spec = DomainSpec(
            mailboxes=boxes,
            catch_all=catch_all,
            provider=provider,
            behaviour=behaviour,
            latency_ms=latency_ms,
            greylist_retries=greylist_retries,
            mx_hosts=hosts,
            accepts_mail=accepts_mail,
        )
        self.domains[d] = spec
        return spec

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> MailWorld:
        """World from the fixture manifest shared with ``FixtureVerifier`` (``email.domains``)."""
        world = cls()
        domains = ((manifest or {}).get("email") or {}).get("domains") or {}
        for name, spec in domains.items():
            if not spec.get("mx", True):
                continue
            world.add(
                name,
                mailboxes=list(spec.get("mailboxes", [])),
                catch_all=bool(spec.get("catch_all", False)),
                provider=MailProvider(spec.get("provider", "unknown")),
                behaviour=spec.get("behaviour", "normal"),
            )
            if spec.get("smtp", True) is False:
                world.domains[normalize_domain(name) or name].behaviour = "timeout"
        return world

    @classmethod
    def from_manifest_path(cls, path: str | Path) -> MailWorld:
        return cls.from_manifest(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- ground truth ------------------------------------------------------------------------
    def spec(self, domain: str) -> DomainSpec | None:
        return self.domains.get(normalize_domain(domain) or domain.lower())

    def mailbox_exists(self, address: str) -> bool:
        addr = normalize_address(address)
        if addr is None:
            return False
        spec = self.spec(split_address(addr)[1])
        return spec is not None and spec.accepts_mail and addr in spec.mailboxes

    def mx_hosts(self, domain: str) -> list[str]:
        spec = self.spec(domain)
        return list(spec.mx_hosts) if spec is not None and spec.accepts_mail else []

    def reset_stats(self) -> None:
        self.sessions = self.connections = self.rcpt_commands = self.simulated_latency_ms = 0
        self.sessions_by_domain.clear()
        self.rcpt_attempts.clear()

    # ---- simulated replies ------------------------------------------------------------------
    def rcpt_reply(self, spec: DomainSpec, domain: str, address: str) -> tuple[int, str]:
        self.rcpt_attempts[address] += 1
        if spec.behaviour == "policy_block":
            return SPAMHAUS
        if spec.behaviour == "accept_all_gateway":
            return ACCEPTED
        if spec.behaviour == "greylist_first" and self.rcpt_attempts[address] <= spec.greylist_retries:
            code, msg = GREYLISTED
            return code, msg.format(addr=address, domain=domain)
        if spec.catch_all or address in spec.mailboxes:
            return ACCEPTED
        if spec.provider == MailProvider.google_workspace:
            return GOOGLE_UNKNOWN
        if spec.provider == MailProvider.microsoft_365:
            return MICROSOFT_DBEB
        code, msg = POSTFIX_UNKNOWN
        return code, msg.format(addr=address)


class WorldDeepVerifier(BatchProber):
    """``DeepVerifier`` backed by a ``MailWorld`` (deterministic: seeded random probes, simulated time)."""

    name = "world"

    def __init__(
        self,
        world: MailWorld,
        *,
        monitor: SmtpHealthMonitor | None = None,
        record_health: bool | None = None,
        sleep: bool = False,
        time_scale: float = 1.0,
        max_targets_per_session: int = 3,
        mx_lookup: MxLookup | None = None,
    ) -> None:
        super().__init__(
            monitor=monitor,
            record_health=(monitor is not None) if record_health is None else record_health,
            max_targets_per_session=max_targets_per_session,
            rng=Random(world.seed),
            mx_lookup=mx_lookup,
        )
        self.world = world
        self.sleep = sleep
        self.time_scale = time_scale
        self._last_domain: str | None = None

    async def resolve_mx(self, domain: str) -> MxInfo:
        """The world's DNS (used by the deep job instead of real MX lookups)."""
        d = normalize_domain(domain) or domain.lower()
        spec = self.world.spec(d)
        if spec is None:
            return MxInfo(d, has_mx=False, has_a=False, error="NXDOMAIN")
        if not spec.accepts_mail:
            return MxInfo(d, has_mx=False, has_a=True, error="null_mx", null_mx=True)
        return MxInfo(d, has_mx=True, mx_hosts=list(spec.mx_hosts), has_a=True)

    def disabled_reason(self) -> str | None:
        return "SMTP probing is disabled in this world" if self.world.smtp_disabled else None

    async def _resolve_hosts(self, domain: str, mx_hosts: list[str]) -> tuple[list[str], str | None]:
        spec = self.world.spec(domain)
        if spec is None:
            return [], "no MX and no A record: the domain accepts no mail"
        if not spec.accepts_mail:
            return [], "null MX (RFC 7505): the domain accepts no mail"
        hosts = [h for h in mx_hosts if h] or spec.mx_hosts
        return hosts, None

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
    ) -> ProbeReport:
        d = normalize_domain(domain) or domain.lower()
        spec = self.world.spec(d)
        if not self.world.smtp_disabled and spec is not None and spec.accepts_mail:
            self.world.sessions += 1
            self.world.sessions_by_domain[d] += 1
        return await super().probe_domain(
            domain,
            mx_hosts,
            addresses,
            check_catch_all=check_catch_all,
            random_probes=random_probes,
            provider=provider,
            catch_all_addresses=catch_all_addresses,
        )

    async def _elapse(self, ms: int) -> None:
        self.world.simulated_latency_ms += ms
        if self.sleep and ms > 0:
            await asyncio.sleep(ms / 1000.0 * self.time_scale)

    async def _connect_and_probe(self, host: str, recipients: list[str], *, domain: str) -> ConnectionResult:
        world = self.world
        spec = world.spec(domain)
        conn = ConnectionResult(host=host, tls="plain")
        world.connections += 1
        if spec is None:  # pragma: no cover - filtered by _resolve_hosts
            conn.outcome, conn.unanswered_result, conn.error = (
                SessionOutcome.infra_failure,
                SmtpResult.unknown,
                "no such domain",
            )
            return conn
        if world.port25_blocked or spec.behaviour == "timeout":
            conn.outcome = SessionOutcome.infra_failure
            conn.unanswered_result = SmtpResult.timeout
            conn.error = f"timeout (connect): connection to {host}:25 timed out"
            conn.elapsed_ms = world.timeout_ms
            await self._elapse(world.timeout_ms)
            return conn
        setup = 3 * spec.latency_ms  # greeting + EHLO + MAIL FROM
        if spec.behaviour == "temporary_always":
            conn.outcome = SessionOutcome.temporary
            conn.unanswered_result = SmtpResult.temporary
            conn.error = f"connect: {TRY_LATER[0]} {TRY_LATER[1]}"
            conn.elapsed_ms = spec.latency_ms
            await self._elapse(spec.latency_ms)
            return conn
        for rcpt in recipients:
            conn.rcpt_sent += 1
            world.rcpt_commands += 1
            code, msg = world.rcpt_reply(spec, domain, rcpt)
            conn.replies.append((rcpt, code, msg))
        conn.elapsed_ms = setup + spec.latency_ms * len(recipients)
        await self._elapse(conn.elapsed_ms)
        return conn
