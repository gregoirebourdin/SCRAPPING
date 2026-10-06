"""Deep path end-to-end on Postgres: request queue, per-domain batching, retries, health gate, job dedupe."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.enums import EmailStatus, JobStatus, MailProvider, SmtpHealthState, SmtpResult
from scout.db.enums import VerificationRequestStatus as RS
from scout.db.models import DomainProfile, Email, EmailVerificationRequest, Job, Person, SmtpHealth
from scout.email import deep
from scout.email.contracts import DomainIntel, DomainProbeResult, SessionOutcome, Verdict
from scout.email.smtp import deep_verifiers as dv
from scout.email.smtp import health
from scout.email.smtp import session as session_mod
from scout.email.smtp.health import DbHealthStore, SmtpHealthMonitor
from scout.email.smtp.world import MailWorld, WorldDeepVerifier
from scout.jobs.worker import Worker

pytestmark = pytest.mark.integration

NAMES = [
    ("Anne", "Martin"),
    ("Bruno", "Leroy"),
    ("Chloe", "Petit"),
    ("David", "Moreau"),
    ("Emma", "Girard"),
    ("Felix", "Roux"),
    ("Gaelle", "Fournier"),
    ("Hugo", "Lambert"),
    ("Ines", "Bonnet"),
    ("Jules", "Francois"),
]


def candidates(first: str, last: str, domain: str) -> list[dict[str, Any]]:
    f, lst = first.lower(), last.lower()
    return [
        {"address": f"{f}.{lst}@{domain}", "pattern": "{first}.{last}", "resolver": "domain_pattern"},
        {"address": f"{f[0]}.{lst}@{domain}", "pattern": "{f}.{last}", "resolver": "permutation"},
        {"address": f"{f[0]}{lst}@{domain}", "pattern": "{f}{last}", "resolver": "permutation"},
    ]


class StubConcluder:
    """Minimal conclusion: accepted on a proven non-catch-all domain → SAFE; temporary → retry; never INVALID
    unless the mailbox itself was rejected."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.persisted: list[tuple[uuid.UUID, Verdict | None, DomainProbeResult | None, SmtpHealthState]] = []

    def conclude(
        self,
        candidates: list[dict[str, Any]],
        intel: DomainIntel | None,
        probe: DomainProbeResult | None,
        *,
        smtp_state: SmtpHealthState,
        attempt: int,
        max_attempts: int,
        first_name: str | None,
        last_name: str | None,
    ) -> tuple[Verdict | None, bool]:
        self.calls.append(
            {
                "addresses": [c["address"] for c in candidates],
                "probe": probe,
                "smtp_state": smtp_state,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "names": (first_name, last_name),
                "catch_all": probe.catch_all if probe else None,
            }
        )
        assert probe is not None
        vs = [probe.verdicts.get(c["address"]) for c in candidates]
        temporary = probe.session == SessionOutcome.temporary or any(
            v is not None and v.result in (SmtpResult.temporary, SmtpResult.timeout) for v in vs
        )
        retry = temporary and attempt < max_attempts
        accepted = next(
            (c["address"] for c, v in zip(candidates, vs) if v and v.result == SmtpResult.accepted), None
        )
        catch_all = probe.catch_all if probe.catch_all is not None else (intel.catch_all if intel else None)
        if accepted and catch_all is False:
            return Verdict(accepted, EmailStatus.SAFE, 0.95), False
        if all(v is not None and v.result == SmtpResult.rejected for v in vs):
            return Verdict(candidates[0]["address"], EmailStatus.INVALID, 0.02), False
        status = EmailStatus.TEMPORARY_UNKNOWN if retry else EmailStatus.RISKY
        return Verdict(candidates[0]["address"], status, 0.5), retry

    async def persist(self, request, verdict, probe, *, smtp_state, intel) -> None:
        self.persisted.append((request.id, verdict, probe, smtp_state))


@pytest.fixture
def stub() -> StubConcluder:
    return StubConcluder()


@pytest.fixture
async def env(db, monkeypatch):
    session_mod.reset_session_state()
    # Hermetic: no intel-layer builds (website / GitHub / DNS) — the deep job falls back to the
    # profile row and the verifier's own MX resolution (the simulated world's DNS).
    monkeypatch.setattr(deep, "_intel_hook", lambda name: None)
    mon = SmtpHealthMonitor(DbHealthStore(), cache_ttl_s=0)
    health.set_monitor(mon)
    yield mon
    health.set_monitor(None)
    dv.set_deep_verifier(None)
    dv.set_default_world(None)
    deep.set_concluder(None)


async def make_people(ws: uuid.UUID, n: int, *, offset: int = 0) -> list[tuple[uuid.UUID, str, str]]:
    out = []
    async with session_scope() as s:
        for i in range(n):
            first, last = NAMES[(offset + i) % len(NAMES)]
            suffix = "" if (offset + i) < len(NAMES) else str(offset + i)
            p = Person(
                workspace_id=ws,
                first_name=first,
                last_name=last + suffix,
                full_name=f"{first} {last}{suffix}",
                normalized_name=f"{first} {last}{suffix}".lower(),
            )
            s.add(p)
            await s.flush()
            out.append((p.id, first, last + suffix))
    return out


async def request_for(ws, people, domain) -> list[uuid.UUID]:
    ids = []
    for pid, first, last in people:
        ids.append(
            await deep.request_deep_verification(
                workspace_id=ws,
                person_id=pid,
                company_id=None,
                campaign_id=None,
                domain=domain,
                candidates=candidates(first, last, domain),
                provisional_status=EmailStatus.RISKY,
                provisional_confidence=0.55,
                deliver_context={"campaign": "c1", "person": str(pid)},
            )
        )
    return ids


async def rows(domain: str | None = None) -> list[EmailVerificationRequest]:
    async with session_scope() as s:
        q = sa.select(EmailVerificationRequest).order_by(EmailVerificationRequest.created_at)
        if domain:
            q = q.where(EmailVerificationRequest.domain == domain)
        return list((await s.scalars(q)).all())


async def jobs() -> list[Job]:
    async with session_scope() as s:
        return list(
            (await s.scalars(sa.select(Job).where(Job.type == deep.JOB_TYPE).order_by(Job.created_at))).all()
        )


async def make_due(domain: str) -> None:
    async with session_scope() as s:
        await s.execute(
            sa.update(EmailVerificationRequest)
            .where(EmailVerificationRequest.domain == domain, EmailVerificationRequest.status == RS.retry)
            .values(next_attempt_at=sa.func.now() - timedelta(seconds=1))
        )


# ---- requests ------------------------------------------------------------------------------------


async def test_request_upsert_and_one_job_per_domain(env, workspace):
    ws, _ = workspace
    people = await make_people(ws, 3)
    ids = await request_for(ws, people, "Acme.FR")
    reqs = await rows()
    assert (
        len(reqs) == 3
        and {r.status for r in reqs} == {RS.pending}
        and {r.domain for r in reqs} == {"acme.fr"}
    )
    assert reqs[0].candidates[0]["address"] == "anne.martin@acme.fr" and len(reqs[0].candidates) == 3
    assert reqs[0].deliver_context == {"campaign": "c1", "person": str(people[0][0])}
    js = await jobs()
    assert (
        len(js) == 1 and js[0].dedupe_key == "email.deep:acme.fr" and js[0].payload == {"domain": "acme.fr"}
    )

    # same candidates while a retry is scheduled: the backoff is kept (never defeat greylisting)
    later = datetime.now(UTC) + timedelta(minutes=30)
    async with session_scope() as s:
        await s.execute(
            sa.update(EmailVerificationRequest)
            .where(EmailVerificationRequest.id == ids[0])
            .values(status=RS.retry, attempts=1, next_attempt_at=later)
        )
    pid, first, last = people[0]
    again = await deep.request_deep_verification(
        workspace_id=ws,
        person_id=pid,
        company_id=None,
        campaign_id=None,
        domain="acme.fr",
        candidates=candidates(first, last, "acme.fr"),
        provisional_status=EmailStatus.LIKELY_SAFE,
        provisional_confidence=0.8,
    )
    r0 = next(r for r in await rows() if r.id == ids[0])
    assert (
        again == ids[0]
        and r0.status == RS.retry
        and r0.attempts == 1
        and r0.provisional_status == EmailStatus.LIKELY_SAFE
    )
    assert abs((r0.next_attempt_at - later).total_seconds()) < 1

    # new candidates restart the request
    new = [{"address": "ANNE@acme.fr"}, {"address": "x@other.fr"}, *candidates(first, last, "acme.fr")]
    await deep.request_deep_verification(
        workspace_id=ws,
        person_id=pid,
        company_id=None,
        campaign_id=None,
        domain="acme.fr",
        candidates=new,
        provisional_status=EmailStatus.RISKY,
        provisional_confidence=0.5,
    )
    r0 = next(r for r in await rows() if r.id == ids[0])
    assert r0.status == RS.pending and r0.attempts == 0 and r0.next_attempt_at <= datetime.now(UTC)
    assert [c["address"] for c in r0.candidates] == [
        "anne@acme.fr",
        "anne.martin@acme.fr",
        "a.martin@acme.fr",
    ]
    assert len(await rows()) == 3

    with pytest.raises(ValueError):
        await deep.request_deep_verification(
            workspace_id=ws,
            person_id=pid,
            company_id=None,
            campaign_id=None,
            domain="acme.fr",
            candidates=[{"address": "x@other.fr"}],
            provisional_status=EmailStatus.RISKY,
            provisional_confidence=0.5,
        )


# ---- batching ------------------------------------------------------------------------------------


async def test_twenty_requests_pilot_then_one_rcpt_each(env, workspace, stub):
    """Unknown convention: one pilot person is probed first, then one RCPT per other person on the confirmed
    pattern — two sessions for 20 people instead of 20 × 3 guesses."""
    ws, _ = workspace
    world = MailWorld()
    people = await make_people(ws, 20)
    world.add(
        "acme.fr",
        mailboxes={f"{f.lower()}.{lst.lower()}" for _, f, lst in people[::2]},
        provider=MailProvider.google_workspace,
    )
    await request_for(ws, people, "acme.fr")
    verifier = WorldDeepVerifier(world, monitor=env)
    summary = await deep.process_domain("acme.fr", verifier=verifier, concluder=stub, monitor=env)

    assert world.sessions == 2 and world.rcpt_commands == 3 + 2 + 19  # pilot's 3 guesses + 2 random, then 1 each
    assert world.connections == 1 + 7  # ≤ 3 targets per connection
    assert summary["claimed"] == 20 and summary["done"] == 20 and summary["probed"]
    assert len(stub.calls) == 20 and len(stub.persisted) == 20
    for call in stub.calls:
        assert set(call["probe"].verdicts) == set(call["addresses"])  # per-request view
        assert call["attempt"] == 1 and call["max_attempts"] == deep.MAX_ATTEMPTS == 4
        assert call["catch_all"] is False and call["names"][0] is not None
    statuses = [v.status for _, v, _, _ in stub.persisted if v is not None]
    assert statuses.count(EmailStatus.SAFE) == 10
    reqs = await rows()
    assert {r.status for r in reqs} == {RS.done} and {r.attempts for r in reqs} == {1}
    assert reqs[0].result["final"] and reqs[0].result["verdict"]["status"] == "SAFE"
    assert reqs[0].result["probe"]["session"] == "ok" and reqs[0].result["probe"]["verifier"] == "world"
    async with session_scope() as s:
        prof = await s.get(DomainProfile, "acme.fr")
        hrow = await s.get(SmtpHealth, "global")
    assert (
        prof is not None and prof.catch_all is False and prof.catch_all_method == "smtp_random_probes:world"
    )
    assert prof.smtp_reachable is True and prof.catch_all_checked_at is not None
    assert hrow is not None and len(hrow.window) == 2  # one health entry per session: pilot, then the rest
    assert summary.get("followup_job") is None


async def test_greylisting_retries_with_backoff_then_concludes(env, workspace, stub):
    ws, _ = workspace
    world = MailWorld()
    world.add(
        "grey.fr", mailboxes={"anne.martin"}, behaviour="greylist_first", provider=MailProvider.self_hosted
    )
    people = await make_people(ws, 2)
    await request_for(ws, people, "grey.fr")
    verifier = WorldDeepVerifier(world, monitor=env)

    t0 = datetime.now(UTC)
    s1 = await deep.process_domain("grey.fr", verifier=verifier, concluder=stub, monitor=env)
    assert s1["retry"] == 2 and stub.persisted == []
    reqs = await rows()
    for r in reqs:
        assert r.status == RS.retry and r.attempts == 1 and r.result["final"] is False
        delay = (r.next_attempt_at - t0).total_seconds()
        assert 300 <= delay < 320  # never under the 300 s greylisting default
    slot_jobs = [j for j in await jobs() if "@" in (j.dedupe_key or "")]
    assert len(slot_jobs) == 1 and slot_jobs[0].run_after >= reqs[0].next_attempt_at
    assert slot_jobs[0].run_after - reqs[0].next_attempt_at < timedelta(minutes=1)
    async with session_scope() as s:
        prof = await s.get(DomainProfile, "grey.fr")
    assert prof is not None and prof.greylisting_seen and prof.catch_all is None
    saved = prof.stats["smtp_catch_all_probes"]["addresses"]
    assert len(saved) == 2

    # not due yet: nothing is probed
    n1 = world.sessions  # pilot greylisted → everyone else probed too, so every address starts its greylist timer
    s_early = await deep.process_domain("grey.fr", verifier=verifier, concluder=stub, monitor=env)
    assert s_early["claimed"] == 0 and world.sessions == n1

    await make_due("grey.fr")
    s2 = await deep.process_domain("grey.fr", verifier=verifier, concluder=stub, monitor=env)
    assert s2["done"] == 2 and world.sessions > n1
    second = stub.calls[-2:]
    assert all(c["attempt"] == 2 for c in second)
    assert second[0]["catch_all"] is False  # same random probes passed greylisting on the retry
    verdicts = {v.address: v.status for _, v, _, _ in stub.persisted if v}
    assert verdicts["anne.martin@grey.fr"] == EmailStatus.SAFE
    assert {r.status for r in await rows()} == {RS.done}
    async with session_scope() as s:
        prof = await s.get(DomainProfile, "grey.fr")
    assert prof is not None and prof.catch_all is False and "smtp_catch_all_probes" not in prof.stats


async def test_temporary_forever_ends_with_a_final_verdict_after_max_attempts(env, workspace, stub):
    ws, _ = workspace
    world = MailWorld()
    world.add("busy.fr", mailboxes={"anne.martin"}, behaviour="temporary_always")
    people = await make_people(ws, 1)
    await request_for(ws, people, "busy.fr")
    verifier = WorldDeepVerifier(world, monitor=env)
    delays = []
    for _ in range(3):
        t0 = datetime.now(UTC)
        await deep.process_domain("busy.fr", verifier=verifier, concluder=stub, monitor=env)
        (r,) = await rows()
        assert r.status == RS.retry
        delays.append(round((r.next_attempt_at - t0).total_seconds() / 60))
        await make_due("busy.fr")
    assert delays == [5, 30, 120]
    await deep.process_domain("busy.fr", verifier=verifier, concluder=stub, monitor=env)
    (r,) = await rows()
    assert r.status == RS.done and r.attempts == 4 and r.result["final"]
    last = stub.calls[-1]
    assert last["attempt"] == last["max_attempts"] == 4
    (_, verdict, probe, _) = stub.persisted[-1]
    assert verdict is not None and verdict.status != EmailStatus.INVALID
    assert probe is not None and probe.session == SessionOutcome.temporary
    assert world.sessions == 4


async def test_blocked_health_concludes_without_probing_and_never_invalid(env, workspace, stub):
    ws, _ = workspace
    world = MailWorld(port25_blocked=True)
    domains = ["a.fr", "b.fr", "c.fr", "d.fr", "e.fr", "f.fr"]
    for d in domains:
        world.add(d, mailboxes={"anne.martin"})
    people = await make_people(ws, 6)
    for (pid, first, last), d in zip(people, domains, strict=True):
        await request_for(ws, [(pid, first, last)], d)
    verifier = WorldDeepVerifier(world, monitor=env)
    for d in domains[:5]:
        await deep.process_domain(d, verifier=verifier, concluder=stub, monitor=env)
    async with session_scope() as s:
        g = await s.get(SmtpHealth, "global")
    assert g is not None and g.state == SmtpHealthState.BLOCKED and g.blocked_until is not None
    assert "blocked" in (g.reason or "")
    sessions_before = world.sessions

    summary = await deep.process_domain("f.fr", verifier=verifier, concluder=stub, monitor=env)
    assert world.sessions == sessions_before and not summary["probed"]
    last = stub.calls[-1]
    assert last["smtp_state"] == SmtpHealthState.BLOCKED
    assert last["probe"].session == SessionOutcome.not_attempted
    assert all(v.result == SmtpResult.not_attempted for v in last["probe"].verdicts.values())
    for call in stub.calls:
        assert not any(v.result == SmtpResult.rejected for v in call["probe"].verdicts.values())
    assert all(v is None or v.status != EmailStatus.INVALID for _, v, _, _ in stub.persisted)
    f_req = next(r for r in await rows("f.fr"))
    assert f_req.status == RS.done and f_req.result["smtp_state"] == "BLOCKED"


async def test_disabled_smtp_never_probes(env, workspace, stub):
    ws, _ = workspace
    world = MailWorld(smtp_disabled=True)
    world.add("acme.fr", mailboxes={"anne.martin"})
    await request_for(ws, await make_people(ws, 1), "acme.fr")
    summary = await deep.process_domain(
        "acme.fr", verifier=WorldDeepVerifier(world, monitor=env), concluder=stub, monitor=env
    )
    assert (
        world.sessions == 0
        and not summary["probed"]
        and stub.calls[0]["smtp_state"] == SmtpHealthState.UNKNOWN
    )
    async with session_scope() as s:
        assert await s.get(SmtpHealth, "global") is None  # not_attempted is never recorded


# ---- concurrency / dedupe ------------------------------------------------------------------------


async def test_resubmission_during_processing_is_requeued(env, workspace, stub):
    ws, _ = workspace
    people = await make_people(ws, 1)
    (rid,) = await request_for(ws, people, "acme.fr")
    (claimed,) = await deep.claim_due_requests("acme.fr")
    assert claimed.id == rid and claimed.status == RS.processing
    assert await deep.claim_due_requests("acme.fr") == []  # never claimed twice
    pid = people[0][0]
    await deep.request_deep_verification(
        workspace_id=ws,
        person_id=pid,
        company_id=None,
        campaign_id=None,
        domain="acme.fr",
        candidates=[{"address": "anne@acme.fr"}],
        provisional_status=EmailStatus.RISKY,
        provisional_confidence=0.4,
    )
    (r,) = await rows()
    assert r.status == RS.processing and r.candidates == [{"address": "anne@acme.fr"}]
    assert await deep._finalize(claimed, status=RS.done, attempts=1) is False
    (r,) = await rows()
    assert r.status == RS.pending and r.attempts == 0


async def test_stale_processing_is_reclaimed(env, workspace):
    ws, _ = workspace
    (rid,) = await request_for(ws, await make_people(ws, 1), "acme.fr")
    await deep.claim_due_requests("acme.fr")
    async with session_scope() as s:
        await s.execute(
            sa.update(EmailVerificationRequest)
            .where(EmailVerificationRequest.id == rid)
            .values(updated_at=sa.func.now() - timedelta(minutes=20))
        )
    (again,) = await deep.claim_due_requests("acme.fr")
    assert again.id == rid


async def test_job_dedupe_running_job_and_scheduled_slots(env, workspace):
    ws, _ = workspace
    async with session_scope() as s:
        j1 = await deep.ensure_domain_job(s, workspace_id=ws, domain="acme.fr")
        assert j1 is not None
        assert await deep.ensure_domain_job(s, workspace_id=ws, domain="acme.fr") is None  # pending swallows
        later = datetime.now(UTC) + timedelta(hours=2)
        slot = await deep.ensure_domain_job(s, workspace_id=ws, domain="acme.fr", run_at=later)
        assert slot is not None  # a scheduled retry never blocks immediate work (and vice versa)
        assert await deep.ensure_domain_job(s, workspace_id=ws, domain="acme.fr", run_at=later) is None
        await s.execute(sa.update(Job).where(Job.id == j1).values(status=JobStatus.running))
        follow = await deep.ensure_domain_job(s, workspace_id=ws, domain="acme.fr")
        assert follow is not None and follow != j1
        assert await deep.ensure_domain_job(s, workspace_id=ws, domain="acme.fr") is None
    by_id = {j.id: j for j in await jobs()}
    assert by_id[follow].dedupe_key == f"email.deep:acme.fr:after:{j1}"
    assert by_id[slot].dedupe_key.startswith("email.deep:acme.fr@")
    assert by_id[slot].run_after.second == 0 and by_id[slot].run_after >= later


async def test_worker_runs_the_deep_job(env, workspace, stub):
    ws, _ = workspace
    world = MailWorld()
    people = await make_people(ws, 3)
    world.add("acme.fr", mailboxes={"anne.martin", "bruno.leroy"}, provider=MailProvider.google_workspace)
    dv.set_deep_verifier(WorldDeepVerifier(world, monitor=env))
    deep.set_concluder(stub)
    await request_for(ws, people, "acme.fr")
    await Worker(slots=2, types=[deep.JOB_TYPE]).run_until_idle(timeout_s=30)
    assert {r.status for r in await rows()} == {RS.done}
    assert world.sessions <= 2 and len(stub.persisted) == 3  # pilot + the rest, one job
    (job,) = await jobs()
    assert job.status == JobStatus.completed and job.result and job.result["done"] == 3


async def test_health_state_is_persisted_in_smtp_health(env):
    mon = SmtpHealthMonitor(DbHealthStore(), cache_ttl_s=0, enabled=lambda: True)
    # port 25 cut (infrastructure): global + provider scopes; a provider's *policy* blocks never reach the
    # global scope (see test_health.test_provider_policies_never_block_the_global_path)
    for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr"):
        await mon.record_session(MailProvider.microsoft_365, SessionOutcome.infra_failure, domain=d)
    fresh = SmtpHealthMonitor(DbHealthStore(), cache_ttl_s=0, enabled=lambda: True)
    assert await fresh.current_state(MailProvider.microsoft_365) == SmtpHealthState.BLOCKED
    assert (
        await fresh.current_state(MailProvider.google_workspace) == SmtpHealthState.BLOCKED
    )  # global blocked too
    async with session_scope() as s:
        scopes = {r.scope: r for r in (await s.scalars(sa.select(SmtpHealth))).all()}
    assert set(scopes) == {"global", "provider:microsoft_365"}
    assert (
        scopes["global"].blocked_until is not None and len(scopes["global"].window) == 6
    )  # 5 entries + marker
    # expired cooldown: exactly one canary claim across monitors (row lock)
    async with session_scope() as s:
        await s.execute(sa.update(SmtpHealth).values(blocked_until=sa.func.now() - timedelta(seconds=1)))
    other = SmtpHealthMonitor(DbHealthStore(), cache_ttl_s=0, enabled=lambda: True)
    first = await fresh.gate(MailProvider.microsoft_365, claim_canary=True)
    second = await other.gate(MailProvider.microsoft_365, claim_canary=True)
    assert first.half_open and first.may_probe and not second.may_probe


async def test_engine_concluder_end_to_end(env, workspace):
    """Real engine conclusion + persistence (scout.email.engine) on a simulated domain."""
    ws, _ = workspace
    world = MailWorld()
    world.add("acme.fr", mailboxes={"anne.martin"}, provider=MailProvider.google_workspace)
    ((pid, _first, _last),) = await make_people(ws, 1)
    cand = {
        "address": "anne.martin@acme.fr",
        "resolver": "domain_pattern",
        "method": "known_pattern",
        "base_probability": 0.55,
        "base_detail": "Domain convention {first}.{last}",
        "affinity": {"score": 0.97, "pattern": "{first}.{last}", "reason": "full first and last name"},
        "pattern": "{first}.{last}",
    }
    await deep.request_deep_verification(
        workspace_id=ws,
        person_id=pid,
        company_id=None,
        campaign_id=None,
        domain="acme.fr",
        candidates=[cand],
        provisional_status=EmailStatus.RISKY,
        provisional_confidence=0.55,
    )
    summary = await deep.process_domain(
        "acme.fr", verifier=WorldDeepVerifier(world, monitor=env), monitor=env
    )
    assert summary["done"] == 1, summary
    async with session_scope() as s:
        email = (await s.scalars(sa.select(Email).where(Email.person_id == pid))).one()
    assert email.address == "anne.martin@acme.fr" and email.status == EmailStatus.SAFE
    (r,) = await rows()
    assert r.status == RS.done and r.result["verdict"]["status"] == "SAFE"


async def test_half_open_canary_on_known_domain_resumes_probing(env, workspace, stub):
    ws, _ = workspace
    world = MailWorld()
    world.add("known-google.fr", provider=MailProvider.google_workspace)  # profile says: not catch-all
    world.add("acme.fr", mailboxes={"anne.martin"}, provider=MailProvider.google_workspace)
    async with session_scope() as s:
        s.add(
            DomainProfile(
                domain="known-google.fr",
                provider=MailProvider.google_workspace,
                mx_hosts=["aspmx.l.google.com"],
                accepts_mail=True,
                catch_all=False,
                catch_all_checked_at=datetime.now(UTC),
            )
        )
    mon = SmtpHealthMonitor(DbHealthStore(), cache_ttl_s=0, enabled=lambda: True)
    for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr"):
        await mon.record_session(None, SessionOutcome.infra_failure, domain=d)
    async with session_scope() as s:  # the cooldown is over
        await s.execute(sa.update(SmtpHealth).values(blocked_until=sa.func.now() - timedelta(seconds=1)))
    await request_for(ws, await make_people(ws, 1), "acme.fr")
    summary = await deep.process_domain(
        "acme.fr", verifier=WorldDeepVerifier(world, monitor=mon), concluder=stub, monitor=mon, canary=True
    )
    assert world.sessions_by_domain["known-google.fr"] == 1  # one cheap canary (1 random RCPT)
    assert world.sessions_by_domain["acme.fr"] == 1 and summary["probed"]
    assert stub.calls[0]["smtp_state"] != SmtpHealthState.BLOCKED
    async with session_scope() as s:
        g = await s.get(SmtpHealth, "global")
    # canary succeeded → window reset to it, then the real probe of acme.fr was recorded
    assert g is not None and g.state == SmtpHealthState.UNKNOWN and g.blocked_until is None
    assert [e.get("d") for e in g.window if e.get("o") != "cooldown"] == ["known-google.fr", "acme.fr"]
