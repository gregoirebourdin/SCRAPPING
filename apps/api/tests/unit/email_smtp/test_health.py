"""SMTP health monitor: window, thresholds, cooldown doubling, half-open canary, provider breaker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scout.db.enums import MailProvider
from scout.db.enums import SmtpHealthState as H
from scout.email.contracts import SessionOutcome as S
from scout.email.smtp import health
from scout.email.smtp.health import (
    COOLDOWN_BASE_S,
    COOLDOWN_MAX_S,
    HealthRecord,
    MemoryHealthStore,
    SmtpHealthMonitor,
    apply_outcome,
    combine,
    evaluate,
    may_probe,
    prune,
)

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
GOOGLE = MailProvider.google_workspace
MS = MailProvider.microsoft_365


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def mon(clock: Clock) -> SmtpHealthMonitor:
    return SmtpHealthMonitor(MemoryHealthStore(), clock=clock, cache_ttl_s=0, enabled=lambda: True)


async def record(mon: SmtpHealthMonitor, clock: Clock, outcome: S, domain: str, provider=None, n: int = 1):
    state = None
    for _ in range(n):
        state = await mon.record_session(provider, outcome, domain=domain)
        clock.advance(seconds=1)
    return state


# ---- pure evaluation ---------------------------------------------------------------------------


def entries(*outcomes: tuple[S, str]) -> list[dict]:
    return [{"t": T0.timestamp() + i, "o": o.value, "d": d} for i, (o, d) in enumerate(outcomes)]


def test_evaluate_thresholds():
    assert evaluate(entries(*[(S.ok, "a.fr")] * 4), "global")[0] == H.UNKNOWN
    assert evaluate(entries(*[(S.ok, "a.fr")] * 5), "global")[0] == H.HEALTHY
    two_fail = entries(*[(S.ok, "a.fr")] * 8, (S.infra_failure, "b.fr"), (S.infra_failure, "c.fr"))
    assert evaluate(two_fail, "global")[0] == H.HEALTHY  # 20 %
    three_fail = entries(*[(S.ok, "a.fr")] * 7, *[(S.infra_failure, d) for d in ("b.fr", "c.fr", "d.fr")])
    assert evaluate(three_fail, "global")[0] == H.DEGRADED  # 30 %
    blocked = entries((S.ok, "a.fr"), *[(S.policy_block, d) for d in ("b.fr", "c.fr", "d.fr", "e.fr")])
    state, reason = evaluate(blocked, "global")
    assert state == H.BLOCKED and "4/5" in reason and "4 domain" in reason


def test_temporary_and_greylisting_are_not_failures():
    window = entries(*[(S.temporary, d) for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr", "f.fr")])
    assert evaluate(window, "global")[0] == H.HEALTHY


def test_one_dead_mx_never_marks_us_blocked():
    window = entries(*[(S.infra_failure, "dead-mx.fr")] * 12)
    state, reason = evaluate(window, "global")
    assert state == H.DEGRADED and "not treated as a block" in reason
    two = entries(*[(S.infra_failure, "dead.fr")] * 6, *[(S.infra_failure, "dead2.fr")] * 6)
    assert evaluate(two, "global")[0] == H.DEGRADED


def test_prune_drops_old_entries_and_caps_window():
    old = [{"t": (T0 - timedelta(hours=3)).timestamp(), "o": "ok", "d": "a.fr"}]
    many = [{"t": T0.timestamp() + i, "o": "ok", "d": "a.fr"} for i in range(60)]
    kept = prune(old + many, T0 + timedelta(minutes=5))
    assert len(kept) == 40 and all(e["t"] >= T0.timestamp() for e in kept)


def test_combine_worst_with_unknown_provider_deferring_to_global():
    assert combine(H.HEALTHY, H.BLOCKED) == H.BLOCKED
    assert combine(H.DEGRADED, H.HEALTHY) == H.DEGRADED
    assert combine(H.HEALTHY, H.UNKNOWN) == H.HEALTHY
    assert combine(H.UNKNOWN, None) == H.UNKNOWN
    assert may_probe(H.DEGRADED) and may_probe(H.UNKNOWN) and not may_probe(H.BLOCKED)


def test_apply_outcome_ignores_not_attempted():
    rec = HealthRecord(scope="global")
    apply_outcome(rec, S.not_attempted, "a.fr", T0)
    assert rec.window == [] and rec.state == H.UNKNOWN


# ---- monitor ----------------------------------------------------------------------------------


async def test_unknown_until_enough_sessions_then_healthy(mon, clock):
    assert await mon.current_state() == H.UNKNOWN
    assert await record(mon, clock, S.ok, "a.fr", n=4) == H.UNKNOWN
    assert await record(mon, clock, S.ok, "b.fr") == H.HEALTHY
    gate = await mon.gate(claim_canary=True)
    assert gate.may_probe and gate.state == H.HEALTHY and not gate.half_open


async def test_not_attempted_is_never_counted(mon, clock):
    for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr", "f.fr"):
        await mon.record_session(None, S.not_attempted, domain=d)
    assert await mon.current_state() == H.UNKNOWN
    assert mon.store.records == {}  # type: ignore[attr-defined]


async def test_disabled_smtp_is_unknown_and_never_probes(clock):
    mon = SmtpHealthMonitor(MemoryHealthStore(), clock=clock, cache_ttl_s=0, enabled=lambda: False)
    gate = await mon.gate(GOOGLE, claim_canary=True)
    assert gate.state == H.UNKNOWN and not gate.may_probe and "disabled" in (gate.reason or "")
    # explicit override (e.g. a simulated / service verifier) still consults the window
    assert (await mon.gate(enabled=True)).may_probe


async def test_dead_mx_on_one_domain_does_not_block(mon, clock):
    await record(mon, clock, S.ok, "a.fr", n=3)
    state = await record(mon, clock, S.infra_failure, "dead-mx.fr", n=20)
    assert state == H.DEGRADED
    assert (await mon.gate(claim_canary=True)).may_probe


async def test_port25_blocked_everywhere_blocks_with_cooldown(mon, clock):
    domains = ["a.fr", "b.fr", "c.fr", "d.fr", "e.fr"]
    for d in domains:
        state = await mon.record_session(None, S.infra_failure, domain=d)
    assert state == H.BLOCKED
    rec = mon.store.records["global"]  # type: ignore[attr-defined]
    assert rec.blocked_until == clock.now + timedelta(seconds=COOLDOWN_BASE_S)
    assert "probing paused for 15 min" in (rec.reason or "")
    gate = await mon.gate(claim_canary=True)
    assert gate.state == H.BLOCKED and not gate.may_probe
    # in-flight sessions finishing during the cooldown never unblock
    clock.advance(minutes=5)
    assert await mon.record_session(None, S.ok, domain="z.fr") == H.BLOCKED
    assert await mon.current_state() == H.BLOCKED


async def test_half_open_single_canary_then_cooldown_doubles(mon, clock):
    for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr"):
        await mon.record_session(None, S.policy_block, domain=d)
    expected = COOLDOWN_BASE_S
    for _ in range(7):
        clock.advance(seconds=expected + 1)
        # readers see half-open as DEGRADED but never consume the canary slot
        assert await mon.current_state() == H.DEGRADED
        first = await mon.gate(claim_canary=True)
        second = await mon.gate(claim_canary=True)
        assert first.half_open and first.may_probe
        assert second.state == H.BLOCKED and not second.may_probe  # one canary at a time
        assert await mon.record_session(None, S.infra_failure, domain="canary.fr") == H.BLOCKED
        expected = min(COOLDOWN_MAX_S, expected * 2)
        rec = mon.store.records["global"]  # type: ignore[attr-defined]
        assert rec.blocked_until == clock.now + timedelta(seconds=expected), expected
    assert expected == COOLDOWN_MAX_S  # 15 min → 30 → 60 → 120 → 240 → 360 (cap 6 h)


async def test_canary_success_resumes_probing_and_streak_continues(mon, clock):
    for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr"):
        await mon.record_session(None, S.infra_failure, domain=d)
    clock.advance(seconds=COOLDOWN_BASE_S + 1)
    assert (await mon.gate(claim_canary=True)).half_open
    assert await mon.record_session(None, S.ok, domain="canary.fr") == H.UNKNOWN
    rec = mon.store.records["global"]  # type: ignore[attr-defined]
    assert rec.blocked_until is None and len(rec.entries) == 1 and "canary" in (rec.reason or "")
    assert (await mon.gate(claim_canary=True)).may_probe
    # blocked again within 24 h → the cooldown keeps doubling
    for d in ("f.fr", "g.fr", "h.fr", "i.fr", "j.fr"):
        await mon.record_session(None, S.infra_failure, domain=d)
    rec = mon.store.records["global"]  # type: ignore[attr-defined]
    assert rec.state == H.BLOCKED and rec.blocked_until == clock.now + timedelta(seconds=2 * COOLDOWN_BASE_S)


async def test_expired_canary_lease_can_be_reclaimed(mon, clock):
    for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr"):
        await mon.record_session(None, S.infra_failure, domain=d)
    clock.advance(seconds=COOLDOWN_BASE_S + 1)
    assert (await mon.gate(claim_canary=True)).half_open  # the claimer crashes without probing
    assert not (await mon.gate(claim_canary=True)).may_probe
    clock.advance(seconds=health.CANARY_LEASE_S + 1)
    assert (await mon.gate(claim_canary=True)).half_open


async def test_provider_circuit_breaker_isolates_microsoft(mon, clock):
    await record(mon, clock, S.ok, "g1.fr", provider=GOOGLE, n=10)
    for d in ("m1.fr", "m2.fr", "m3.fr", "m1.fr", "m2.fr"):
        await mon.record_session(MS, S.policy_block, domain=d)
        clock.advance(seconds=1)
    assert await mon.current_state(MS) == H.BLOCKED
    assert await mon.current_state(GOOGLE) != H.BLOCKED
    assert (await mon.gate(GOOGLE, claim_canary=True)).may_probe
    rec = mon.store.records["provider:microsoft_365"]  # type: ignore[attr-defined]
    assert "circuit breaker" in (rec.reason or "")
    assert mon.store.records["global"].state != H.BLOCKED  # type: ignore[attr-defined]


async def test_breaker_needs_two_domains(mon, clock):
    await record(mon, clock, S.ok, "g1.fr", provider=GOOGLE, n=10)
    await record(mon, clock, S.policy_block, "strict-tenant.fr", provider=MS, n=6)
    assert await mon.current_state(MS) != H.BLOCKED


async def test_stale_window_decays_to_unknown(mon, clock):
    await record(mon, clock, S.ok, "a.fr", n=3)
    await record(mon, clock, S.infra_failure, "b.fr", n=3)
    assert await mon.current_state() == H.DEGRADED
    clock.advance(hours=3)
    assert await mon.current_state() == H.UNKNOWN


class CountingStore(MemoryHealthStore):
    def __init__(self) -> None:
        super().__init__()
        self.gets = 0

    async def get(self, scope: str):
        self.gets += 1
        return await super().get(scope)


async def test_in_process_cache_avoids_store_reads(clock):
    store = CountingStore()
    mon = SmtpHealthMonitor(store, clock=clock, cache_ttl_s=60, enabled=lambda: True)
    for _ in range(500):
        await mon.gate(GOOGLE, claim_canary=True)
    assert store.gets == 2  # global + provider scope, once each
    await mon.record_session(GOOGLE, S.ok, domain="a.fr")  # writes refresh the cache
    await mon.gate(GOOGLE)
    assert store.gets == 2


async def test_module_level_api_uses_overridable_monitor(clock):
    mon = SmtpHealthMonitor(MemoryHealthStore(), clock=clock, cache_ttl_s=0, enabled=lambda: True)
    health.set_monitor(mon)
    try:
        for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr"):
            await health.record_session(None, S.infra_failure, domain=d)
        assert await health.current_state() == H.BLOCKED
        assert not (await health.gate(claim_canary=True)).may_probe
    finally:
        health.set_monitor(None)


async def test_provider_policies_never_block_the_global_path(mon, clock):
    # Real Railway run: Microsoft 365 refused us (Spamhaus) and OVH too (no reverse DNS) before any Google
    # session — Google Workspace domains must keep being verified.
    ovh = MailProvider.ovh
    for d, p in (("m1.fr", MS), ("o1.fr", ovh), ("m2.fr", MS), ("o2.fr", ovh), ("m3.fr", MS), ("o3.fr", ovh)):
        await mon.record_session(p, S.policy_block, domain=d)
        clock.advance(seconds=1)
    assert mon.store.records.get("global") is None or mon.store.records["global"].state != H.BLOCKED  # type: ignore[attr-defined]
    assert (await mon.gate(GOOGLE, claim_canary=True)).may_probe
    # an unattributed policy block, or our port 25 being cut, still counts globally
    await record(mon, clock, S.infra_failure, "x1.fr", n=2)
    await record(mon, clock, S.infra_failure, "x2.fr", n=2)
    await record(mon, clock, S.infra_failure, "x3.fr", n=2)
    assert await mon.current_state(GOOGLE) == H.BLOCKED
