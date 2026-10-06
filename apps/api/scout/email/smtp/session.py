"""Batched per-domain SMTP prober: one logical session per domain for every pending candidate.

``probe_domain(domain, mx_hosts, addresses, check_catch_all=…)``:

* targets are split into connections of at most 3 RCPTs (+ the random catch-all probes in the first
  connection): EHLO (HELO fallback) → STARTTLS when offered (certificate verified; on any TLS failure
  the probe reconnects without TLS and notes it) → MAIL FROM → RCPT TO … → QUIT. DATA is never sent.
* MX hosts are walked in preference order, moving on ONLY on an infrastructure failure (connect
  refused / timeout / dropped); no MX → implicit MX via the A record; null MX (RFC 7505) → no probe.
* a session-level refusal (policy block, 4xx) or a 421 during RCPT stops the domain for now: the
  remaining targets get ``blocked`` / ``temporary`` without more connections.
* catch-all: ``random_probes`` plausible-but-nonexistent addresses of different shapes; ``True`` only
  if ALL are accepted, ``False`` if all are rejected as unknown users, otherwise ``None`` (any
  temporary answer → unknown). Accept-then-bounce providers (Yahoo/AOL/Verizon MX, secure gateways,
  SES/Mailgun) always get random probes, and their 250s are downgraded to ``unknown`` unless the random
  probes were rejected.
* connections are serialised per MX HOST (many domains share one MX) with a small minimum spacing,
  inside the bounded ``pool("smtp")``.
* the aggregate session outcome is recorded once per call in the health monitor (``not_attempted``
  never is). This module does NOT consult the health gate: callers do (``health.gate(...,
  claim_canary=True)``) — see ``scout.email.deep`` and ``BuiltinVerifier``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from random import Random
from typing import Any

import aiosmtplib
import structlog

from scout.config import get_settings
from scout.db.enums import MailProvider, SmtpResult
from scout.email import dns
from scout.email.contracts import DomainProbeResult, RcptVerdict, SessionOutcome
from scout.email.smtp.classify import (
    ReplyClass,
    ReplyKind,
    classify_exception,
    classify_rcpt_reply,
    is_greylisting,
    is_policy_reply,
    provider_from_mx,
    random_probe_addresses,
    uninformative_accepts,
)
from scout.email.smtp.health import SmtpHealthMonitor, get_monitor
from scout.email.syntax import normalize_address, normalize_domain, split_address
from scout.util.pools import pool

log = structlog.get_logger(__name__)

SMTP_PORT = 25
MAX_MX_HOSTS = 3
MAX_TARGETS_PER_SESSION = 3
DEFAULT_RANDOM_PROBES = 2
UNINFORMATIVE_CATCH_ALL_CONFIDENCE = 0.3
DOMAIN_TIME_BUDGET_S = 300.0  # wall-clock budget per probe_domain call; the rest is deferred (temporary)

MxLookup = Callable[[str], Awaitable[dns.MxInfo]]
SmtpFactory = Callable[..., Any]

# --------------------------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------------------------


@dataclass
class ProbeReport(DomainProbeResult):
    """``DomainProbeResult`` plus diagnostics (random-probe verdicts, connections, TLS, notes)."""

    random_verdicts: dict[str, RcptVerdict] = field(default_factory=dict)
    connections: int = 0
    hosts_tried: list[str] = field(default_factory=list)
    tls: str | None = None
    notes: list[str] = field(default_factory=list)
    provider: MailProvider | None = None
    uninformative_accepts: bool = False
    greylisted: bool = False


def probe_summary(probe: DomainProbeResult | None, *, max_message: int = 200) -> dict[str, Any] | None:
    """JSON-safe compact summary (stored in ``email_verification_requests.result``)."""
    if probe is None:
        return None

    def v(rv: RcptVerdict) -> dict[str, Any]:
        return {"result": rv.result.value, "code": rv.code, "message": (rv.message or "")[:max_message]}

    out: dict[str, Any] = {
        "domain": probe.domain,
        "session": probe.session.value,
        "mx_host": probe.mx_host,
        "catch_all": probe.catch_all,
        "catch_all_confidence": probe.catch_all_confidence,
        "probes": probe.probes,
        "duration_ms": probe.duration_ms,
        "error": probe.error,
        "verifier": probe.verifier,
        "verdicts": {a: v(rv) for a, rv in probe.verdicts.items()},
    }
    if isinstance(probe, ProbeReport):
        out.update(
            random={a: v(rv) for a, rv in probe.random_verdicts.items()},
            connections=probe.connections,
            hosts_tried=probe.hosts_tried,
            tls=probe.tls,
            notes=probe.notes,
            provider=probe.provider.value if probe.provider else None,
            uninformative_accepts=probe.uninformative_accepts,
            greylisted=probe.greylisted,
        )
    return out


def restrict_probe(probe: DomainProbeResult, addresses: list[str]) -> DomainProbeResult:
    """Per-request view of a domain probe: only these addresses' verdicts (``probes`` = their RCPTs)."""
    wanted = [a for a in addresses if a in probe.verdicts]
    return replace(probe, verdicts={a: probe.verdicts[a] for a in wanted}, probes=len(wanted))


@dataclass
class ConnectionResult:
    """One SMTP connection (≤ 3 targets + random probes)."""

    host: str
    outcome: SessionOutcome = SessionOutcome.ok
    replies: list[tuple[str, int, str]] = field(default_factory=list)  # (rcpt, code, message)
    unanswered_result: SmtpResult | None = None
    error: str | None = None
    tls: str | None = None
    rcpt_sent: int = 0
    rate_limited: bool = False  # 421 during RCPT: leave the domain alone for now
    elapsed_ms: int = 0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Identity validation (HELO / MAIL FROM)
# --------------------------------------------------------------------------------------------

_RESERVED_SUFFIXES = (
    ".local",
    ".localdomain",
    ".localhost",
    ".example",
    ".invalid",
    ".test",
    ".internal",
    ".lan",
    ".home",
    ".corp",
    ".intranet",
    ".home.arpa",
)
_RESERVED_EXACT = frozenset({"localhost", "example.com", "example.net", "example.org"})
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def _reserved(domain: str) -> bool:
    return (
        domain in _RESERVED_EXACT
        or domain.endswith(_RESERVED_SUFFIXES)
        or any(domain.endswith("." + r) for r in _RESERVED_EXACT)
    )


def identity_problem(helo_domain: str | None, mail_from: str | None) -> str | None:
    """Why our SMTP identity must not be used on the Internet (None when it looks like a real, owned FQDN)."""
    h = (helo_domain or "").strip().lower().rstrip(".")
    if not h:
        return "SMTP_HELO_DOMAIN is empty"
    with suppress(ValueError):
        ipaddress.ip_address(h.strip("[]"))
        return f"HELO name {h!r} is an IP literal, not a domain we own"
    labels = h.split(".")
    if len(labels) < 2 or not all(_LABEL.match(lbl) for lbl in labels) or labels[-1].isdigit():
        return f"HELO name {h!r} is not a fully qualified domain name"
    if _reserved(h):
        return f"HELO name {h!r} is a reserved / unowned domain (.local, .example, localhost…)"
    addr = normalize_address(mail_from or "")
    if addr is None:
        return f"SMTP_FROM_ADDRESS {mail_from!r} is not a valid address"
    _, from_domain = split_address(addr)
    if _reserved(from_domain):
        return f"MAIL FROM domain {from_domain!r} is a reserved / unowned domain"
    return None


# --------------------------------------------------------------------------------------------
# Per-MX-host serialisation + STARTTLS memory
# --------------------------------------------------------------------------------------------

_MAX_TRACKED_HOSTS = 10_000
_mx_locks: dict[str, asyncio.Lock] = {}
_mx_last: dict[str, float] = {}
_mx_loop: asyncio.AbstractEventLoop | None = None
_tls_broken: set[str] = set()


def _mx_lock(host: str) -> asyncio.Lock:
    global _mx_loop
    loop = asyncio.get_running_loop()
    if _mx_loop is not loop:
        _mx_locks.clear()
        _mx_loop = loop
    lock = _mx_locks.get(host)
    if lock is None:
        if len(_mx_locks) >= _MAX_TRACKED_HOSTS:
            for key in [k for k, v in _mx_locks.items() if not v.locked()]:
                _mx_locks.pop(key, None)
                _mx_last.pop(key, None)
        lock = _mx_locks[host] = asyncio.Lock()
    return lock


@asynccontextmanager
async def mx_host_slot(host: str, spacing_s: float) -> AsyncIterator[None]:
    """One connection at a time per MX host, at least ``spacing_s`` apart."""
    async with _mx_lock(host):
        wait = _mx_last.get(host, 0.0) + spacing_s - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            yield
        finally:
            _mx_last[host] = time.monotonic()


def _remember_tls_broken(host: str) -> None:
    if len(_tls_broken) >= _MAX_TRACKED_HOSTS:
        _tls_broken.clear()
    _tls_broken.add(host)


def reset_session_state() -> None:
    """Tests: forget MX locks, spacing and STARTTLS memory."""
    global _mx_loop
    _mx_locks.clear()
    _mx_last.clear()
    _tls_broken.clear()
    _mx_loop = None


# --------------------------------------------------------------------------------------------
# Batch orchestration (shared by the real prober and the simulated world)
# --------------------------------------------------------------------------------------------


def _clean_host(host: str) -> str:
    return (host or "").strip().rstrip(".").lower()


class BatchProber:
    """Per-domain batching, MX walking, catch-all analysis and health recording.

    Subclasses implement ``_connect_and_probe`` (one connection) and may override ``disabled_reason``
    and ``_resolve_hosts``.
    """

    name = "batch"

    def __init__(
        self,
        *,
        monitor: SmtpHealthMonitor | None = None,
        record_health: bool = True,
        max_targets_per_session: int = MAX_TARGETS_PER_SESSION,
        max_mx_hosts: int = MAX_MX_HOSTS,
        rng: Random | None = None,
        mx_lookup: MxLookup | None = None,
        time_budget_s: float = DOMAIN_TIME_BUDGET_S,
    ) -> None:
        self._monitor = monitor
        self.record_health = record_health
        self.max_targets_per_session = max(1, max_targets_per_session)
        self.max_mx_hosts = max(1, max_mx_hosts)
        self._rng = rng
        self._mx_lookup = mx_lookup
        self.time_budget_s = time_budget_s

    @property
    def monitor(self) -> SmtpHealthMonitor:
        return self._monitor or get_monitor()

    @property
    def enabled(self) -> bool:
        return self.disabled_reason() is None

    def disabled_reason(self) -> str | None:
        return None

    async def _connect_and_probe(self, host: str, recipients: list[str], *, domain: str) -> ConnectionResult:
        raise NotImplementedError

    async def _resolve_hosts(self, domain: str, mx_hosts: list[str]) -> tuple[list[str], str | None]:
        hosts = list(dict.fromkeys(h for h in (_clean_host(x) for x in mx_hosts) if h))
        if hosts:
            return hosts, None
        lookup = self._mx_lookup or dns.mx_lookup
        try:
            info = await lookup(domain)
        except Exception as exc:
            return [], f"MX lookup failed: {exc}"
        if info.null_mx:
            return [], "null MX (RFC 7505): the domain accepts no mail"
        if info.transient:
            return [], f"MX lookup inconclusive ({info.error})"
        if info.mx_hosts:
            return [_clean_host(h) for h in info.mx_hosts], None
        if info.has_a:
            return [domain], None  # implicit MX (RFC 5321 §5.1)
        return [], "no MX and no A record: the domain accepts no mail"

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
                continue
            if addr not in targets:
                targets.append(addr)

        def skip(reason: str) -> ProbeReport:
            for a in targets:
                report.verdicts[a] = RcptVerdict(a, SmtpResult.not_attempted, None, reason)
            report.error = reason
            report.duration_ms = int((time.monotonic() - t0) * 1000)
            return report

        reason = self.disabled_reason()
        if reason is not None:
            return skip(reason)
        hosts, host_error = await self._resolve_hosts(d, mx_hosts)
        if not hosts:
            return skip(host_error or "no mail host")
        prov = provider if provider is not None else provider_from_mx(hosts, domain=d)
        report.provider = prov
        uninformative = uninformative_accepts(prov, hosts)
        report.uninformative_accepts = uninformative

        randoms: list[str] = []
        if (check_catch_all or uninformative) and random_probes > 0:
            given = [normalize_address(a) for a in (catch_all_addresses or [])]
            randoms = [a for a in dict.fromkeys(given) if a and split_address(a)[1] == d and a not in targets]
            randoms = randoms[:random_probes]
            if len(randoms) < random_probes:
                randoms += [
                    a
                    for a in random_probe_addresses(d, random_probes, rng=self._rng)
                    if a not in randoms and a not in targets
                ][: random_probes - len(randoms)]
        if not targets and not randoms:
            return skip("nothing to probe")

        k = self.max_targets_per_session
        chunks = [targets[i : i + k] for i in range(0, len(targets), k)] or [[]]
        conns: list[ConnectionResult] = []
        answered: dict[str, tuple[int, str]] = {}
        unanswered: dict[str, tuple[SmtpResult, str]] = {}
        host_idx = 0
        stop: tuple[SmtpResult, str] | None = None
        max_hosts = min(len(hosts), self.max_mx_hosts)

        for ci, chunk in enumerate(chunks):
            rcpts = chunk + (randoms if ci == 0 else [])
            if stop is None and ci > 0 and time.monotonic() - t0 > self.time_budget_s:
                stop = (SmtpResult.temporary, "deferred: per-domain probing time budget exhausted")
                report.notes.append("time budget exhausted: remaining targets deferred")
            if stop is not None or host_idx >= max_hosts:
                fallback = stop or (SmtpResult.unknown, "no reachable MX host")
                unanswered.update(dict.fromkeys(rcpts, fallback))
                continue
            conn: ConnectionResult | None = None
            while host_idx < max_hosts:
                host = hosts[host_idx]
                conn = await self._connect_and_probe(host, rcpts, domain=d)
                conns.append(conn)
                if host not in report.hosts_tried:
                    report.hosts_tried.append(host)
                report.notes.extend(conn.notes)
                if conn.outcome == SessionOutcome.infra_failure and not conn.replies:
                    host_idx += 1  # walk the MX list only on infrastructure failures
                    continue
                break
            assert conn is not None
            for rcpt, code, message in conn.replies:
                answered[rcpt] = (code, message)
            rest = [r for r in rcpts if r not in answered]
            failure = (conn.unanswered_result or SmtpResult.unknown, conn.error or conn.outcome.value)
            unanswered.update(dict.fromkeys(rest, failure))
            if not conn.replies and conn.outcome != SessionOutcome.ok:
                stop = failure  # the server refused the session (or every MX is down): same for the rest
            elif conn.rate_limited:
                stop = (SmtpResult.temporary, "server asked to slow down (421): deferred")
                report.notes.append(f"421 from {conn.host}: remaining targets deferred")

        self._assemble(report, targets, randoms, answered, unanswered, conns, prov, uninformative)
        report.duration_ms = sum(c.elapsed_ms for c in conns) or int((time.monotonic() - t0) * 1000)
        if self.record_health and report.session != SessionOutcome.not_attempted:
            try:
                await self.monitor.record_session(prov, report.session, domain=d, probes=report.probes)
            except Exception as exc:  # health accounting never breaks a probe
                log.warning("email.smtp.health_record_failed", domain=d, error=str(exc))
        log.debug(
            "email.smtp.probe",
            domain=d,
            session=report.session,
            targets=len(targets),
            randoms=len(randoms),
            connections=report.connections,
            catch_all=report.catch_all,
            ms=report.duration_ms,
        )
        return report

    @staticmethod
    def _assemble(
        report: ProbeReport,
        targets: list[str],
        randoms: list[str],
        answered: dict[str, tuple[int, str]],
        unanswered: dict[str, tuple[SmtpResult, str]],
        conns: list[ConnectionResult],
        provider: MailProvider | None,
        uninformative: bool,
    ) -> None:
        classes: dict[str, ReplyClass] = {
            a: classify_rcpt_reply(code, msg, provider=provider) for a, (code, msg) in answered.items()
        }

        def verdict(a: str) -> RcptVerdict:
            if a in answered:
                code, msg = answered[a]
                return RcptVerdict(a, classes[a].result, code, msg)
            result, detail = unanswered.get(a, (SmtpResult.unknown, "no answer"))
            return RcptVerdict(a, result, None, detail)

        for a in targets:
            report.verdicts[a] = verdict(a)
        for a in randoms:
            report.random_verdicts[a] = verdict(a)

        # ---- catch-all ----------------------------------------------------------------------
        rv = [report.random_verdicts[a] for a in randoms]
        catch_all: bool | None = None
        confidence: float | None = None
        if rv:
            strength = 0.95 if len(rv) >= 2 else 0.85
            if any(v.result in (SmtpResult.temporary, SmtpResult.timeout) for v in rv) or any(
                a not in answered for a in randoms
            ):
                report.notes.append("catch-all unknown: random probes were not answered conclusively")
            elif all(v.result == SmtpResult.accepted for v in rv):
                catch_all, confidence = True, strength
            elif all(v.result == SmtpResult.rejected for v in rv):
                catch_all, confidence = False, strength
            else:
                share = sum(v.result == SmtpResult.accepted for v in rv) / len(rv)
                confidence = round(share, 2) if share > 0 else None
        if uninformative and catch_all is not False:
            catch_all, confidence = None, UNINFORMATIVE_CATCH_ALL_CONFIDENCE
            report.notes.append("accept-then-bounce provider: RCPT acceptance does not prove a mailbox")
            for a in targets:
                cur = report.verdicts[a]
                if (
                    cur.result == SmtpResult.accepted
                    and classes.get(a)
                    and classes[a].kind == ReplyKind.accepted
                ):
                    report.verdicts[a] = RcptVerdict(
                        a, SmtpResult.unknown, cur.code, f"uninformative (accept-then-bounce): {cur.message}"
                    )
        report.catch_all, report.catch_all_confidence = catch_all, confidence

        # ---- session outcome ----------------------------------------------------------------
        replied = list(classes.values())
        if replied:
            all_blocked = all(c.result == SmtpResult.blocked for c in replied)
            report.session = SessionOutcome.policy_block if all_blocked else SessionOutcome.ok
        elif conns:
            outcomes = {c.outcome for c in conns}
            for o in (SessionOutcome.policy_block, SessionOutcome.temporary, SessionOutcome.infra_failure):
                if o in outcomes:
                    report.session = o
                    break
        report.greylisted = any(c.kind == ReplyKind.greylisted for c in replied) or any(
            is_greylisting(c.error or "") for c in conns
        )
        report.connections = len(conns)
        report.probes = sum(c.rcpt_sent for c in conns)
        first_ok = next((c for c in conns if c.replies), None)
        report.mx_host = first_ok.host if first_ok else (conns[-1].host if conns else None)
        report.error = next((c.error for c in conns if c.error), None)
        report.tls = next((c.tls for c in conns if c.tls), None)


# --------------------------------------------------------------------------------------------
# Real prober (port 25, aiosmtplib)
# --------------------------------------------------------------------------------------------


def _supports(client: Any, extension: str) -> bool:
    fn = getattr(client, "supports_extension", None)
    if fn is None:
        return False
    try:
        return bool(fn(extension))
    except Exception:
        return False


async def _close(client: Any) -> None:
    with suppress(Exception):
        if getattr(client, "is_connected", False):
            await asyncio.wait_for(client.quit(), timeout=2.0)
    with suppress(Exception):
        client.close()


class SmtpProber(BatchProber):
    """RCPT-only prober over port 25 (aiosmtplib)."""

    name = "builtin"

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        helo_domain: str | None = None,
        mail_from: str | None = None,
        timeout: float | None = None,
        port: int = SMTP_PORT,
        smtp_factory: SmtpFactory | None = None,
        use_starttls: bool = True,
        allow_reserved_identity: bool | None = None,
        mx_spacing_s: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        s = get_settings()
        self._enabled = s.smtp_enabled if enabled is None else enabled
        self.helo_domain = helo_domain or s.smtp_helo_domain
        self.mail_from = mail_from or s.smtp_from_address
        self.timeout = timeout if timeout is not None else s.smtp_timeout
        self.port = port
        self._factory: SmtpFactory = smtp_factory or aiosmtplib.SMTP
        self.use_starttls = use_starttls
        self.allow_reserved_identity = (
            (s.app_env == "test") if allow_reserved_identity is None else allow_reserved_identity
        )
        self.mx_spacing_s = s.per_domain_delay_ms / 1000.0 if mx_spacing_s is None else mx_spacing_s
        self._identity_warned = False

    def disabled_reason(self) -> str | None:
        if not self._enabled:
            return "SMTP probing is disabled (SMTP_ENABLED=false)"
        if not self.allow_reserved_identity:
            problem = identity_problem(self.helo_domain, self.mail_from)
            if problem is not None:
                if not self._identity_warned:
                    self._identity_warned = True
                    log.warning(
                        "email.smtp.identity_refused",
                        problem=problem,
                        hint="set SMTP_HELO_DOMAIN / SMTP_FROM_ADDRESS to a domain you own (PTR + SPF)",
                    )
                return f"SMTP identity refused: {problem}"
        return None

    def _new_client(self, host: str) -> Any:
        return self._factory(
            hostname=host,
            port=self.port,
            timeout=self.timeout,
            local_hostname=self.helo_domain,
            use_tls=False,
            start_tls=False,
            validate_certs=True,
        )

    @staticmethod
    async def _hello(client: Any) -> None:
        """EHLO, falling back to HELO when EHLO is not understood (never after a policy refusal)."""
        try:
            await client.ehlo()
        except aiosmtplib.SMTPHeloError as exc:
            if (
                500 <= exc.code < 600
                and not is_policy_reply(exc.code, exc.message)
                and hasattr(client, "helo")
                and getattr(client, "is_connected", True)
            ):
                await client.helo()
            else:
                raise

    async def _connect_and_probe(self, host: str, recipients: list[str], *, domain: str) -> ConnectionResult:
        async with mx_host_slot(host, self.mx_spacing_s), pool("smtp"):
            return await self._run(host, recipients)

    async def _run(self, host: str, recipients: list[str]) -> ConnectionResult:
        t0 = time.monotonic()
        conn = ConnectionResult(host=host)
        client = self._new_client(host)
        stage = "connect"
        try:
            async with asyncio.timeout(self.timeout * (4 + len(recipients))):
                await client.connect()
                stage = "ehlo"
                await self._hello(client)
                if self.use_starttls and host not in _tls_broken and _supports(client, "starttls"):
                    stage = "starttls"
                    try:
                        await client.starttls(validate_certs=True)
                        await self._hello(client)  # RFC 3207: forget pre-TLS state, EHLO again
                        conn.tls = "starttls"
                    except Exception as exc:
                        conn.tls = f"failed: {type(exc).__name__}"
                        conn.notes.append(
                            f"STARTTLS failed on {host} ({type(exc).__name__}): continued without TLS"
                        )
                        _remember_tls_broken(host)
                        await _close(client)
                        client = self._new_client(host)
                        stage = "connect"
                        await client.connect()
                        stage = "ehlo"
                        await self._hello(client)
                else:
                    conn.tls = conn.tls or "plain"
                stage = "mail"
                await client.mail(self.mail_from)
                stage = "rcpt"
                for rcpt in recipients:
                    conn.rcpt_sent += 1
                    try:
                        resp = await client.rcpt(rcpt)
                        code, message = resp.code, resp.message
                    except aiosmtplib.SMTPRecipientRefused as exc:
                        code, message = exc.code, exc.message
                    conn.replies.append((rcpt, code, (message or "")[:500]))
                    if code == 421:  # the server closes the connection: stop here
                        conn.rate_limited = True
                        conn.unanswered_result = SmtpResult.temporary
                        conn.error = f"421 {message}"[:500]
                        break
        except Exception as exc:
            failure = classify_exception(exc, stage=stage)
            conn.error = failure.detail[:500]
            if conn.replies:  # targets were answered; a later failure only loses the remaining RCPTs
                conn.unanswered_result = conn.unanswered_result or failure.result
            else:
                conn.outcome = failure.outcome
                conn.unanswered_result = failure.result
            log.info("email.smtp.session_failed", host=host, stage=stage, error=conn.error)
        finally:
            await _close(client)
        conn.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return conn


# --------------------------------------------------------------------------------------------
# Module-level convenience
# --------------------------------------------------------------------------------------------

_default_prober: SmtpProber | None = None


def get_prober() -> SmtpProber:
    global _default_prober
    if _default_prober is None:
        _default_prober = SmtpProber()
    return _default_prober


def set_prober(prober: SmtpProber | None) -> None:
    global _default_prober
    _default_prober = prober


async def probe_domain(
    domain: str,
    mx_hosts: list[str],
    addresses: list[str],
    *,
    check_catch_all: bool,
    random_probes: int = DEFAULT_RANDOM_PROBES,
    provider: MailProvider | None = None,
    catch_all_addresses: list[str] | None = None,
) -> DomainProbeResult:
    """Probe every address of one domain with the process-wide builtin prober (see module doc)."""
    return await get_prober().probe_domain(
        domain,
        mx_hosts,
        addresses,
        check_catch_all=check_catch_all,
        random_probes=random_probes,
        provider=provider,
        catch_all_addresses=catch_all_addresses,
    )
