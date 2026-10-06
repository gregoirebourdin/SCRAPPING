"""Health of OUR SMTP verification path — never confused with mailbox validity.

Scopes: ``global`` and ``provider:<MailProvider>`` (table ``smtp_health``). Every probe session records
ONE outcome (``ok`` / ``temporary`` / ``infra_failure`` / ``policy_block``; ``not_attempted`` is never
recorded) into a bounded sliding window (last 40 sessions, at most 2 h old). States:

* ``UNKNOWN``  — fewer than 5 sessions in the window (also: SMTP probing disabled).
* ``BLOCKED``  — infra_failure + policy_block ≥ 80 % of ≥ 5 sessions spanning ≥ 3 distinct domains
                 (one dead MX can never mark us blocked), or — provider scopes only — a circuit breaker:
                 the last 5 sessions are all policy blocks across ≥ 2 domains (Microsoft blocking us
                 must not stall Google domains). Probing stops until ``blocked_until``: cooldown 15 min,
                 doubling on each consecutive block (within 24 h) up to 6 h.
* ``DEGRADED`` — failures ≥ 30 % (probing continues).
* ``HEALTHY``  — otherwise.

Half-open: once the cooldown has expired, readers see ``DEGRADED`` and exactly one prober may claim
the canary slot (``gate(..., claim_canary=True)``; a 2-minute lease protects against a crashed claimer).
The next recorded session decides: success → window reset, state ``UNKNOWN`` (re-evaluated as data
comes in); failure → ``BLOCKED`` again with a doubled cooldown.

Window format (JSONB list): session entries ``{"t": epoch, "o": outcome, "d": domain}`` plus at most one
cooldown marker ``{"t": epoch, "o": "cooldown", "s": seconds, "n": streak, "canary": epoch|None}``.

A few-second in-process cache keeps thousands of probes from reading the table each time.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scout.config import get_settings
from scout.db.enums import MailProvider, SmtpHealthState
from scout.email.contracts import SMTP_USABLE, SessionOutcome

log = structlog.get_logger(__name__)

SCOPE_GLOBAL = "global"

WINDOW_MAX = 40
WINDOW_MAX_AGE_S = 2 * 3600
MIN_SESSIONS = 5
BLOCK_RATIO = 0.8
DEGRADE_RATIO = 0.3
MIN_BLOCK_DOMAINS = 3
BREAKER_CONSECUTIVE = 5
BREAKER_MIN_DOMAINS = 2
COOLDOWN_BASE_S = 15 * 60
COOLDOWN_MAX_S = 6 * 3600
STREAK_RESET_S = 24 * 3600
CANARY_LEASE_S = 120
CACHE_TTL_S = 5.0

_FAILURES = frozenset({SessionOutcome.infra_failure.value, SessionOutcome.policy_block.value})
_SUCCESSES = frozenset({SessionOutcome.ok.value, SessionOutcome.temporary.value})
_MARKER = "cooldown"
_SEVERITY = {
    SmtpHealthState.HEALTHY: 0,
    SmtpHealthState.UNKNOWN: 1,
    SmtpHealthState.DEGRADED: 2,
    SmtpHealthState.BLOCKED: 3,
}


def provider_scope(provider: MailProvider | str) -> str:
    return f"provider:{MailProvider(provider).value}"


def scopes_for(provider: MailProvider | str | None) -> list[str]:
    """``global`` plus the provider scope when the provider is a real one."""
    out = [SCOPE_GLOBAL]
    if provider is not None:
        p = MailProvider(provider)
        if p not in (MailProvider.unknown, MailProvider.none):
            out.append(provider_scope(p))
    return out


def may_probe(state: SmtpHealthState) -> bool:
    """Whether a state allows probing (everything but BLOCKED). See ``gate`` for the full decision."""
    return state in SMTP_USABLE


# --------------------------------------------------------------------------------------------
# Pure state machine
# --------------------------------------------------------------------------------------------


@dataclass
class HealthRecord:
    scope: str
    state: SmtpHealthState = SmtpHealthState.UNKNOWN
    window: list[dict[str, Any]] = field(default_factory=list)
    blocked_until: datetime | None = None
    reason: str | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    state_changed_at: datetime | None = None

    @property
    def entries(self) -> list[dict[str, Any]]:
        return [e for e in self.window if isinstance(e, dict) and e.get("o") != _MARKER]

    @property
    def marker(self) -> dict[str, Any] | None:
        for e in self.window:
            if isinstance(e, dict) and e.get("o") == _MARKER:
                return e
        return None


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def _fmt_minutes(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m} min" if m < 120 else f"{m / 60:.1f} h"


def prune(entries: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    cutoff = _ts(now) - WINDOW_MAX_AGE_S
    kept = [e for e in entries if float(e.get("t", 0)) >= cutoff and e.get("o") in _FAILURES | _SUCCESSES]
    return kept[-WINDOW_MAX:]


def evaluate(entries: list[dict[str, Any]], scope: str) -> tuple[SmtpHealthState, str]:
    """State + human reason for a (pruned) window of session entries."""
    n = len(entries)
    if n < MIN_SESSIONS:
        return (
            SmtpHealthState.UNKNOWN,
            f"{n} SMTP session(s) in the last 2 h (< {MIN_SESSIONS}): not enough data",
        )
    fails = [e for e in entries if e["o"] in _FAILURES]
    infra = sum(1 for e in fails if e["o"] == SessionOutcome.infra_failure.value)
    policy = len(fails) - infra
    domains = {e.get("d") for e in fails}
    ratio = len(fails) / n
    summary = f"{len(fails)}/{n} recent SMTP sessions failed (infrastructure {infra}, policy {policy}) across {len(domains)} domain(s)"
    if scope != SCOPE_GLOBAL:
        tail = entries[-BREAKER_CONSECUTIVE:]
        tail_domains = {e.get("d") for e in tail}
        if (
            len(tail) == BREAKER_CONSECUTIVE
            and all(e["o"] == SessionOutcome.policy_block.value for e in tail)
            and len(tail_domains) >= BREAKER_MIN_DOMAINS
        ):
            return (
                SmtpHealthState.BLOCKED,
                f"circuit breaker: {BREAKER_CONSECUTIVE} consecutive policy blocks on {scope.split(':', 1)[1]} "
                f"across {len(tail_domains)} domains",
            )
    if ratio >= BLOCK_RATIO and len(domains) >= MIN_BLOCK_DOMAINS:
        return SmtpHealthState.BLOCKED, f"{summary}: our SMTP path looks blocked"
    if ratio >= DEGRADE_RATIO:
        extra = (
            f" (concentrated on {len(domains)} domain(s): not treated as a block of our infrastructure)"
            if ratio >= BLOCK_RATIO
            else ""
        )
        return SmtpHealthState.DEGRADED, f"{summary}{extra}"
    return SmtpHealthState.HEALTHY, summary


def _cooldown(marker: dict[str, Any] | None, now: datetime) -> tuple[int, float]:
    """(streak, cooldown seconds) for a new block, doubling while blocks keep recurring within 24 h."""
    streak = 0
    if marker is not None and _ts(now) - float(marker.get("t", 0)) < STREAK_RESET_S:
        streak = int(marker.get("n", 0)) + 1
    return streak, float(min(COOLDOWN_MAX_S, COOLDOWN_BASE_S * (2**streak)))


def _block(rec: HealthRecord, entries: list[dict[str, Any]], now: datetime, reason: str) -> None:
    streak, cd = _cooldown(rec.marker, now)
    rec.blocked_until = now + timedelta(seconds=cd)
    marker = {"t": _ts(now), "o": _MARKER, "s": cd, "n": streak, "canary": None}
    rec.window = [*entries, marker]
    if rec.state != SmtpHealthState.BLOCKED:
        rec.state_changed_at = now
    rec.state = SmtpHealthState.BLOCKED
    rec.reason = (
        f"{reason}; probing paused for {_fmt_minutes(cd)} (until {rec.blocked_until:%Y-%m-%d %H:%M} UTC)"
    )


def is_half_open(rec: HealthRecord, now: datetime) -> bool:
    """BLOCKED whose cooldown has expired, or whose canary slot has been claimed."""
    if rec.state != SmtpHealthState.BLOCKED:
        return False
    marker = rec.marker
    if marker is not None and marker.get("canary"):
        return True
    return rec.blocked_until is None or rec.blocked_until <= now


def apply_outcome(rec: HealthRecord, outcome: SessionOutcome, domain: str, now: datetime) -> HealthRecord:
    """Record one session outcome and recompute the state (mutates and returns ``rec``)."""
    if outcome == SessionOutcome.not_attempted:
        return rec
    entry = {"t": _ts(now), "o": outcome.value, "d": domain}
    success = outcome.value in _SUCCESSES
    if success:
        rec.last_success_at = now
    else:
        rec.last_failure_at = now
    marker = rec.marker
    entries = prune([*rec.entries, entry], now)

    if rec.state == SmtpHealthState.BLOCKED:
        if not is_half_open(rec, now):
            # In-flight session finishing during the cooldown: remember it, stay BLOCKED.
            rec.window = [*entries, *([marker] if marker else [])]
            return rec
        if success:
            if marker is not None:
                marker = {**marker, "canary": None}
            rec.window = [entry, *([marker] if marker else [])]
            rec.state = SmtpHealthState.UNKNOWN
            rec.state_changed_at = now
            rec.blocked_until = None
            rec.reason = f"canary session on {domain} succeeded after the cooldown: probing resumed"
            return rec
        _block(rec, entries, now, f"canary session on {domain} failed ({outcome.value})")
        return rec

    state, reason = evaluate(entries, rec.scope)
    if state == SmtpHealthState.BLOCKED:
        _block(rec, entries, now, reason)
        return rec
    rec.window = [*entries, *([marker] if marker else [])]
    if state != rec.state:
        rec.state_changed_at = now
    rec.state = state
    rec.reason = reason
    rec.blocked_until = None
    return rec


def claim_canary_slot(rec: HealthRecord, now: datetime) -> bool:
    """Take the single half-open probe slot (lease ``CANARY_LEASE_S``). Mutates ``rec`` when claimed."""
    if rec.state != SmtpHealthState.BLOCKED:
        return False
    if rec.blocked_until is not None and rec.blocked_until > now:
        return False  # still cooling down, or another prober holds the canary lease
    marker = rec.marker or {"t": _ts(now), "o": _MARKER, "s": COOLDOWN_BASE_S, "n": 0}
    marker = {**marker, "canary": _ts(now)}
    rec.window = [*rec.entries, marker]
    rec.blocked_until = now + timedelta(seconds=CANARY_LEASE_S)
    return True


def view(rec: HealthRecord | None, now: datetime) -> tuple[SmtpHealthState, bool]:
    """(state as seen now, cooldown expired → half-open). Stale windows decay to UNKNOWN."""
    if rec is None:
        return SmtpHealthState.UNKNOWN, False
    if rec.state == SmtpHealthState.BLOCKED:
        if rec.blocked_until is not None and rec.blocked_until > now:
            return SmtpHealthState.BLOCKED, False
        return SmtpHealthState.DEGRADED, True
    state, _ = evaluate(prune(rec.entries, now), rec.scope)
    if state == SmtpHealthState.BLOCKED:  # only record_session may enter BLOCKED (it sets the cooldown)
        state = SmtpHealthState.DEGRADED
    return state, False


def combine(global_state: SmtpHealthState, provider_state: SmtpHealthState | None) -> SmtpHealthState:
    """Worst of both; a provider scope without data (UNKNOWN) defers to the global scope."""
    if provider_state is None or provider_state == SmtpHealthState.UNKNOWN:
        return global_state
    return max(global_state, provider_state, key=lambda s: _SEVERITY[s])


# --------------------------------------------------------------------------------------------
# Stores
# --------------------------------------------------------------------------------------------

Mutator = Callable[[HealthRecord], bool]  # mutates in place, returns whether to persist


class HealthStore(Protocol):
    async def get(self, scope: str) -> HealthRecord | None: ...

    async def mutate(self, scope: str, fn: Mutator) -> HealthRecord: ...


class MemoryHealthStore:
    """In-process store (tests, benchmarks, ``BuiltinVerifier(use_db_cache=False)``)."""

    def __init__(self) -> None:
        self.records: dict[str, HealthRecord] = {}
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def get(self, scope: str) -> HealthRecord | None:
        rec = self.records.get(scope)
        return copy.deepcopy(rec) if rec is not None else None

    async def mutate(self, scope: str, fn: Mutator) -> HealthRecord:
        async with self._get_lock():
            rec = copy.deepcopy(self.records.get(scope)) or HealthRecord(scope=scope)
            if fn(rec):
                self.records[scope] = copy.deepcopy(rec)
            return rec


class DbHealthStore:
    """``smtp_health`` rows, read-modify-write under ``SELECT … FOR UPDATE``."""

    async def get(self, scope: str) -> HealthRecord | None:
        from scout.db.engine import session_scope
        from scout.db.models import SmtpHealth

        async with session_scope() as s:
            row = await s.get(SmtpHealth, scope)
            return _to_record(row) if row is not None else None

    async def mutate(self, scope: str, fn: Mutator) -> HealthRecord:
        from scout.db.engine import session_scope
        from scout.db.models import SmtpHealth

        async with session_scope() as s:
            await s.execute(
                pg_insert(SmtpHealth).values(scope=scope).on_conflict_do_nothing(index_elements=["scope"])
            )
            row = (
                await s.execute(sa.select(SmtpHealth).where(SmtpHealth.scope == scope).with_for_update())
            ).scalar_one()
            rec = _to_record(row)
            if fn(rec):
                row.state = rec.state
                row.window = list(rec.window)
                row.blocked_until = rec.blocked_until
                row.reason = rec.reason
                row.last_success_at = rec.last_success_at
                row.last_failure_at = rec.last_failure_at
                row.state_changed_at = rec.state_changed_at
            return rec


def _to_record(row: Any) -> HealthRecord:
    return HealthRecord(
        scope=row.scope,
        state=SmtpHealthState(row.state),
        window=[dict(e) for e in (row.window or []) if isinstance(e, dict)],
        blocked_until=row.blocked_until,
        reason=row.reason,
        last_success_at=row.last_success_at,
        last_failure_at=row.last_failure_at,
        state_changed_at=row.state_changed_at,
    )


# --------------------------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthGate:
    """The probing decision for one caller."""

    state: SmtpHealthState
    may_probe: bool
    half_open: bool = False  # this caller holds the canary slot: its next session decides
    reason: str | None = None
    scopes: dict[str, SmtpHealthState] = field(default_factory=dict)


class SmtpHealthMonitor:
    def __init__(
        self,
        store: HealthStore | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        cache_ttl_s: float = CACHE_TTL_S,
        enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.store: HealthStore = store if store is not None else DbHealthStore()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache_ttl = cache_ttl_s
        self._cache: dict[str, tuple[float, HealthRecord | None]] = {}
        self._enabled = enabled or (lambda: get_settings().smtp_enabled)
        self.probes_since_canary = 0
        self.last_canary_at: float | None = None  # time.monotonic()

    def now(self) -> datetime:
        return self._clock()

    def invalidate_cache(self) -> None:
        self._cache.clear()

    async def _load(self, scope: str) -> HealthRecord | None:
        hit = self._cache.get(scope)
        if hit is not None and time.monotonic() - hit[0] < self._cache_ttl:
            return hit[1]
        try:
            rec = await self.store.get(scope)
        except Exception as exc:  # health must never break verification
            log.warning("email.smtp.health_read_failed", scope=scope, error=str(exc))
            return hit[1] if hit is not None else None
        self._cache[scope] = (time.monotonic(), rec)
        return rec

    def _remember(self, rec: HealthRecord) -> None:
        self._cache[rec.scope] = (time.monotonic(), rec)

    async def record_session(
        self, provider: MailProvider | None, outcome: SessionOutcome, *, domain: str, probes: int = 1
    ) -> SmtpHealthState:
        """Append one session outcome (global + provider scope) and return the resulting combined state."""
        if outcome == SessionOutcome.not_attempted:
            return await self.current_state(provider)
        now = self.now()
        states: dict[str, SmtpHealthState] = {}
        for scope in scopes_for(provider):
            before: dict[str, SmtpHealthState] = {}

            def mutate(rec: HealthRecord, _before: dict[str, SmtpHealthState] = before) -> bool:
                _before["state"] = rec.state
                apply_outcome(rec, outcome, domain, now)
                return True

            try:
                rec = await self.store.mutate(scope, mutate)
            except Exception as exc:
                log.warning("email.smtp.health_write_failed", scope=scope, error=str(exc))
                continue
            self._remember(rec)
            states[scope] = view(rec, now)[0]
            if before.get("state") != rec.state:
                log.info(
                    "email.smtp.health_changed",
                    scope=scope,
                    previous=before.get("state"),
                    state=rec.state,
                    reason=rec.reason,
                    blocked_until=rec.blocked_until.isoformat() if rec.blocked_until else None,
                )
        self.probes_since_canary += max(0, probes)
        provider_states = [v for k, v in states.items() if k != SCOPE_GLOBAL]
        return combine(
            states.get(SCOPE_GLOBAL, SmtpHealthState.UNKNOWN), provider_states[0] if provider_states else None
        )

    async def gate(
        self,
        provider: MailProvider | None = None,
        *,
        claim_canary: bool = False,
        enabled: bool | None = None,
    ) -> HealthGate:
        """Whether to probe now. Probers pass ``claim_canary=True``; readers (UI, fast path) must not."""
        if not (self._enabled() if enabled is None else enabled):
            return HealthGate(SmtpHealthState.UNKNOWN, False, reason="SMTP probing is disabled")
        now = self.now()
        states: dict[str, SmtpHealthState] = {}
        reasons: list[str] = []
        half_open = False
        for scope in scopes_for(provider):
            rec = await self._load(scope)
            st, expired = view(rec, now)
            if expired and claim_canary:
                claimed: dict[str, bool] = {}

                def take(r: HealthRecord, _c: dict[str, bool] = claimed) -> bool:
                    _c["ok"] = claim_canary_slot(r, now)
                    return _c["ok"]

                try:
                    fresh = await self.store.mutate(scope, take)
                except Exception as exc:
                    log.warning("email.smtp.health_write_failed", scope=scope, error=str(exc))
                    fresh = rec or HealthRecord(scope=scope)
                    claimed["ok"] = False
                self._remember(fresh)
                if claimed.get("ok"):
                    half_open = True
                    st = SmtpHealthState.DEGRADED
                    reasons.append(f"{scope}: half-open, this caller runs the canary probe")
                    log.info("email.smtp.canary_claimed", scope=scope)
                else:
                    st = view(fresh, now)[0]
            if rec is not None and rec.reason and st != SmtpHealthState.HEALTHY:
                reasons.append(f"{scope}: {rec.reason}")
            states[scope] = st
        provider_state = next((v for k, v in states.items() if k != SCOPE_GLOBAL), None)
        state = combine(states[SCOPE_GLOBAL], provider_state)
        return HealthGate(
            state=state,
            may_probe=may_probe(state),
            half_open=half_open and may_probe(state),
            reason="; ".join(reasons) or None,
            scopes=states,
        )

    async def current_state(
        self,
        provider: MailProvider | None = None,
        *,
        claim_canary: bool = False,
        enabled: bool | None = None,
    ) -> SmtpHealthState:
        """Worst of global and provider scope (expired cooldown → ``DEGRADED``, i.e. half-open)."""
        return (await self.gate(provider, claim_canary=claim_canary, enabled=enabled)).state


# --------------------------------------------------------------------------------------------
# Process-wide monitor
# --------------------------------------------------------------------------------------------

_monitor: SmtpHealthMonitor | None = None


def get_monitor() -> SmtpHealthMonitor:
    global _monitor
    if _monitor is None:
        _monitor = SmtpHealthMonitor()
    return _monitor


def set_monitor(monitor: SmtpHealthMonitor | None) -> None:
    """Override (tests, benchmarks) or reset (None → DB-backed monitor on next use)."""
    global _monitor
    _monitor = monitor


async def record_session(
    provider: MailProvider | None, outcome: SessionOutcome, *, domain: str, probes: int = 1
) -> SmtpHealthState:
    return await get_monitor().record_session(provider, outcome, domain=domain, probes=probes)


async def current_state(
    provider: MailProvider | None = None, *, claim_canary: bool = False, enabled: bool | None = None
) -> SmtpHealthState:
    return await get_monitor().current_state(provider, claim_canary=claim_canary, enabled=enabled)


async def gate(
    provider: MailProvider | None = None, *, claim_canary: bool = False, enabled: bool | None = None
) -> HealthGate:
    return await get_monitor().gate(provider, claim_canary=claim_canary, enabled=enabled)
