"""Email persistence: pattern memory, findings/checks, user-confirmed rows, DNS cache, jobs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import dns.name
import pytest
import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.enums import EmailDiscoveryMethod as M
from scout.db.enums import EmailKind
from scout.db.enums import EmailStatus as S
from scout.db.enums import SmtpResult as R
from scout.db.models import (
    Company,
    DomainDnsCache,
    DomainEmailPattern,
    Email,
    EmailCheck,
    EmailVerificationRequest,
    JobEvent,
    Person,
    WebsitePage,
)
from scout.email import dns as edns
from scout.email import store
from scout.email.jobs import email_find, email_verify
from scout.email.types import EmailFinding
from scout.email.verifier import set_verifier
from scout.errors import NotFound
from scout.jobs.registry import JobContext, get_handler
from tests.unit.email.fakes import fixture_verifier, vr

pytestmark = pytest.mark.integration


# ---- helpers --------------------------------------------------------------------------------


async def make_company(ws, domain, *, employee_max=20, country="FR"):
    async with session_scope() as s:
        c = Company(
            workspace_id=ws,
            name=domain,
            normalized_name=domain,
            domain=domain,
            normalized_domain=domain,
            employee_max=employee_max,
            country=country,
        )
        s.add(c)
        await s.flush()
        return c.id


async def make_person(ws, company_id, first, last):
    async with session_scope() as s:
        p = Person(
            workspace_id=ws,
            company_id=company_id,
            first_name=first,
            last_name=last,
            full_name=f"{first} {last}",
            normalized_name=f"{first} {last}".lower(),
        )
        s.add(p)
        await s.flush()
        return p.id


async def add_page(ws, company_id, url, emails):
    async with session_scope() as s:
        s.add(
            WebsitePage(
                workspace_id=ws,
                company_id=company_id,
                url=url,
                canonical_url=url,
                content_hash=url,
                emails=emails,
            )
        )


async def checks_for(email_id):
    async with session_scope() as s:
        return (await s.scalars(sa.select(EmailCheck).where(EmailCheck.email_id == email_id))).all()


async def get_email(ws, address):
    async with session_scope() as s:
        return await s.scalar(sa.select(Email).where(Email.workspace_id == ws, Email.address == address))


async def get_person(pid):
    async with session_scope() as s:
        return await s.get(Person, pid)


async def pattern_row(domain, pattern):
    async with session_scope() as s:
        return await s.scalar(
            sa.select(DomainEmailPattern).where(
                DomainEmailPattern.domain == domain, DomainEmailPattern.pattern == pattern
            )
        )


def finding(address, status=S.SAFE, conf=0.95, *, method=M.permutation, smtp=R.accepted, catch_all=False):
    return EmailFinding(
        address=address,
        status=status,
        overall_confidence=conf,
        method=method,
        pattern="{first}.{last}",
        pattern_confidence=0.4,
        verification=vr(address, smtp=smtp, catch_all=catch_all),
    )


@pytest.fixture
def verifier():
    v = fixture_verifier()
    set_verifier(v)
    yield v
    set_verifier(None)


# ---- domain pattern memory --------------------------------------------------------------------


async def test_learn_and_load_patterns(db):
    counts = await store.learn_patterns(
        "Agence-X.fr",
        [
            ("Marie", "Dupont", "marie.dupont"),
            ("Jean", "Martin", "jean.martin"),
            ("Paul", "Durand", "pdurand"),
            ("Zoé", "Roux", "contact"),
        ],
    )
    assert counts == {"{first}.{last}": 2, "{f}{last}": 1}
    # Learning v2 (scout.email.intel.learning): posterior predictive confidences compete (sum ≤ 1).
    patterns = await store.load_domain_patterns("agence-x.fr")
    assert [(p, n) for p, _, n in patterns] == [("{first}.{last}", 2), ("{f}{last}", 1)]
    assert patterns[0][1] == pytest.approx(0.767, abs=0.01) and patterns[1][1] < 0.3
    await store.learn_patterns("agence-x.fr", [("Léa", "Petit", "lea.petit")])
    row = await pattern_row("agence-x.fr", "{first}.{last}")
    assert row.supporting_samples == 3 and row.confidence > patterns[0][1]
    await store.learn_patterns("agence-x.fr", [("Léa", "Petit", "lea.petit")])  # same address: idempotent
    assert (await pattern_row("agence-x.fr", "{first}.{last}")).supporting_samples == 3
    assert await store.load_domain_patterns(None) == []


async def test_record_pattern_outcome(db):
    await store.record_pattern_outcome("studio-y.fr", "{first}", success=True)
    row = await pattern_row("studio-y.fr", "{first}")
    assert row.successful_checks == 1 and 0.6 < row.confidence < 0.9
    assert row.last_verified_at is not None and row.last_confirmed_at is not None

    await store.learn_patterns("studio-y.fr", [("Marie", "Dupont", "mdupont"), ("Jean", "Martin", "jmartin")])
    before = (await pattern_row("studio-y.fr", "{f}{last}")).confidence
    await store.record_pattern_outcome("studio-y.fr", "{f}{last}", success=False)
    row = await pattern_row("studio-y.fr", "{f}{last}")
    assert row.failed_checks == 1 and row.confidence < before and row.last_failed_at is not None

    # Failure counters are kept even without samples, but never surface as a known pattern.
    await store.record_pattern_outcome("studio-y.fr", "{last}", success=False)
    row = await pattern_row("studio-y.fr", "{last}")
    assert row.failed_checks == 1 and row.share == 0 and row.supporting_samples == 0
    assert "{last}" not in [p for p, _, _ in await store.load_domain_patterns("studio-y.fr")]
    await store.record_pattern_outcome("studio-y.fr", "{bogus}", success=True)
    assert await pattern_row("studio-y.fr", "{bogus}") is None


# ---- findings ----------------------------------------------------------------------------------


async def test_save_finding_upserts_and_appends_checks(workspace):
    ws, _ = workspace
    cid = await make_company(ws, "agence-x.fr")
    pid = await make_person(ws, cid, "Marie", "Dupont")

    row = await store.save_finding(
        ws, person_id=pid, company_id=cid, finding=finding("Marie.Dupont@agence-x.fr")
    )
    assert row.address == "marie.dupont@agence-x.fr" and row.local_part == "marie.dupont"
    assert row.kind == EmailKind.person and row.status == S.SAFE and row.smtp_result == R.accepted
    assert row.catch_all is False and row.is_primary and row.last_checked_at is not None
    assert (await get_person(pid)).primary_email_id == row.id

    again = await store.save_finding(
        ws,
        person_id=pid,
        company_id=cid,
        finding=finding("marie.dupont@agence-x.fr", S.CATCH_ALL, 0.4, smtp=R.accepted, catch_all=True),
    )
    assert again.id == row.id and again.status == S.CATCH_ALL and again.catch_all is True
    checks = await checks_for(row.id)
    assert len(checks) == 2 and {c.status for c in checks} == {S.SAFE, S.CATCH_ALL}
    assert {c.result["signals"]["smtp_result"] for c in checks} == {"accepted"}
    assert all(c.verifier == "fake" and "raw" in c.result for c in checks)


async def test_user_confirmed_rows_are_not_overwritten(workspace):
    ws, _ = workspace
    cid = await make_company(ws, "agence-x.fr")
    pid = await make_person(ws, cid, "Marie", "Dupont")
    row = await store.save_finding(
        ws, person_id=pid, company_id=cid, finding=finding("marie.dupont@agence-x.fr")
    )
    async with session_scope() as s:
        await s.execute(sa.update(Email).where(Email.id == row.id).values(is_user_confirmed=True))

    kept = await store.save_finding(
        ws,
        person_id=pid,
        company_id=cid,
        finding=finding("marie.dupont@agence-x.fr", S.INVALID, 0.02, smtp=R.rejected),
    )
    assert kept.status == S.SAFE and kept.smtp_result == R.accepted and kept.is_primary
    assert len(await checks_for(row.id)) == 2  # history still recorded


async def test_primary_is_not_replaced_by_a_worse_finding(workspace):
    ws, _ = workspace
    cid = await make_company(ws, "agence-x.fr")
    pid = await make_person(ws, cid, "Marie", "Dupont")
    safe = await store.save_finding(
        ws, person_id=pid, company_id=cid, finding=finding("marie.dupont@agence-x.fr")
    )
    worse = await store.save_finding(
        ws,
        person_id=pid,
        company_id=cid,
        finding=finding("marie@agence-x.fr", S.UNKNOWN, 0.38, smtp=R.not_attempted, catch_all=None),
    )
    assert not worse.is_primary
    assert (await get_person(pid)).primary_email_id == safe.id


async def test_address_owned_by_another_person(workspace):
    ws, _ = workspace
    cid = await make_company(ws, "agence-x.fr")
    marie = await make_person(ws, cid, "Marie", "Dupont")
    other = await make_person(ws, cid, "Mario", "Dupont")
    await store.save_finding(ws, person_id=marie, company_id=cid, finding=finding("m.dupont@agence-x.fr"))
    assert (
        await store.save_finding(ws, person_id=other, company_id=cid, finding=finding("m.dupont@agence-x.fr"))
        is None
    )
    assert (
        await store.save_finding(
            ws,
            person_id=other,
            company_id=cid,
            finding=EmailFinding(None, S.UNKNOWN, 0.0, None, None, None, None),
        )
        is None
    )


async def test_save_company_email(workspace):
    ws, _ = workspace
    cid = await make_company(ws, "agence-x.fr")
    row = await store.save_company_email(ws, cid, "Contact@Agence-X.fr", "https://agence-x.fr/contact")
    assert row.kind == EmailKind.role and row.person_id is None and row.role_address
    assert row.discovery_method == M.published and row.status == S.UNKNOWN
    generic = await store.save_company_email(ws, cid, "agencex.paris@gmail.com", None)
    assert generic.kind == EmailKind.generic and generic.free_provider
    assert (await store.save_company_email(ws, cid, "contact@agence-x.fr", None)).id == row.id
    assert await store.save_company_email(ws, cid, "not-an-email", None) is None


async def test_reverify_updates_status_and_respects_confirmed(workspace):
    ws, _ = workspace
    cid = await make_company(ws, "agence-x.fr")
    pid = await make_person(ws, cid, "Marie", "Dupont")
    # Fixture: marie.dupont@ is deliverable, m.dupont@ is rejected.
    bad = await store.save_finding(ws, person_id=pid, company_id=cid, finding=finding("m.dupont@agence-x.fr"))
    confirmed = await store.save_finding(
        ws, person_id=pid, company_id=cid, finding=finding("dupont@agence-x.fr"), make_primary=False
    )
    async with session_scope() as s:
        await s.execute(sa.update(Email).where(Email.id == confirmed.id).values(is_user_confirmed=True))
    set_verifier(fixture_verifier())
    try:
        updated = await store.reverify(ws, [bad.id, confirmed.id, uuid.uuid4()])
    finally:
        set_verifier(None)
    assert updated == 1
    bad_now = await get_email(ws, "m.dupont@agence-x.fr")
    assert bad_now.status == S.INVALID and bad_now.smtp_result == R.rejected and not bad_now.is_primary
    assert (await get_person(pid)).primary_email_id is None
    conf_now = await get_email(ws, "dupont@agence-x.fr")
    assert conf_now.status == S.SAFE and conf_now.is_user_confirmed
    assert len(await checks_for(bad.id)) == 2 and len(await checks_for(confirmed.id)) == 2
    assert await store.reverify(ws, []) == 0


# ---- DNS cache --------------------------------------------------------------------------------


class CountingResolver:
    def __init__(self):
        self.calls = []

    async def resolve(self, qname, rdtype):
        self.calls.append((qname, rdtype))
        if rdtype == "MX":
            return [SimpleNamespace(preference=10, exchange=dns.name.from_text("mx1.agence-x.fr."))]
        if rdtype == "A":
            return ["192.0.2.1"]
        raise AssertionError(rdtype)


async def test_mx_cache_reused_without_dns_call(db, monkeypatch):
    resolver = CountingResolver()
    monkeypatch.setattr(edns, "resolver_factory", lambda: resolver)
    first = await edns.mx_lookup("agence-x.fr")
    assert first.mx_hosts == ["mx1.agence-x.fr"] and not first.cached
    n = len(resolver.calls)
    second = await edns.mx_lookup("Agence-X.fr")
    assert second.cached and second.mx_hosts == ["mx1.agence-x.fr"] and second.has_a
    assert len(resolver.calls) == n  # served from domain_dns_cache

    async with session_scope() as s:
        await s.execute(sa.update(DomainDnsCache).values(checked_at=datetime.now(UTC) - timedelta(days=31)))
    third = await edns.mx_lookup("agence-x.fr")
    assert not third.cached and len(resolver.calls) > n  # expired → resolved again


async def test_null_mx_is_cached_in_its_own_column(db, monkeypatch):
    class NullMxResolver:
        async def resolve(self, qname, rdtype):
            if rdtype == "MX":
                return [SimpleNamespace(preference=0, exchange=dns.name.from_text("."))]
            return ["192.0.2.1"]

    monkeypatch.setattr(edns, "resolver_factory", lambda: NullMxResolver())
    first = await edns.mx_lookup("nomail.fr")
    assert first.null_mx and not first.accepts_mail
    async with session_scope() as s:
        row = await s.get(DomainDnsCache, "nomail.fr")
        assert row is not None and row.null_mx is True
    cached = await edns.mx_lookup("nomail.fr")
    assert cached.cached and cached.null_mx and not cached.accepts_mail


async def test_catch_all_cache(db):
    assert await edns.cached_catch_all("agence-x.fr") is None
    await edns.store_catch_all("agence-x.fr", True)
    assert await edns.cached_catch_all("Agence-X.fr") is True
    await edns.store_catch_all("agence-x.fr", None)  # inconclusive probes keep the verdict
    assert await edns.cached_catch_all("agence-x.fr") is True
    # A catch-all-only row does not count as an MX cache hit.
    async with session_scope() as s:
        row = await s.get(DomainDnsCache, "agence-x.fr")
        assert row.has_mx is None
        row.catch_all_checked_at = datetime.now(UTC) - timedelta(days=31)
    assert await edns.cached_catch_all("agence-x.fr") is None


# ---- orchestration ------------------------------------------------------------------------------


async def test_find_and_save_published_learns_once(workspace, verifier):
    ws, _ = workspace
    cid = await make_company(ws, "agence-x.fr")
    marie = await make_person(ws, cid, "Marie", "Dupont")
    await make_person(ws, cid, "Jean", "Martin")
    await add_page(
        ws,
        cid,
        "https://agence-x.fr/equipe",
        ["marie.dupont@agence-x.fr", "contact@agence-x.fr", {"address": "jean.martin@agence-x.fr"}],
    )

    f = await store.find_and_save_for_person(ws, marie)
    assert f.status == S.SAFE and f.method == M.published and f.address == "marie.dupont@agence-x.fr"
    assert f.source_url == "https://agence-x.fr/equipe" and verifier.calls == ["marie.dupont@agence-x.fr"]
    row = await get_email(ws, "marie.dupont@agence-x.fr")
    assert row.is_primary and row.discovery_method == M.published and row.person_id == marie
    assert (await pattern_row("agence-x.fr", "{first}.{last}")).supporting_samples == 1

    await store.find_and_save_for_person(ws, marie)  # re-run: no double counting
    assert (await pattern_row("agence-x.fr", "{first}.{last}")).supporting_samples == 1
    assert len(await checks_for(row.id)) == 2


async def test_find_and_save_guess_records_pattern_outcomes(workspace, verifier):
    ws, _ = workspace
    cid = await make_company(ws, "studio-y.fr", employee_max=8)
    paul = await make_person(ws, cid, "Paul", "Durand")
    await store.learn_patterns("studio-y.fr", [("Zoé", "Roux", "zroux")])  # stale known pattern {f}{last}

    f = await store.find_and_save_for_person(ws, paul)
    assert verifier.calls == ["pdurand@studio-y.fr", "paul@studio-y.fr"]
    assert f.status == S.SAFE and f.address == "paul@studio-y.fr" and f.method == M.permutation
    stale = await pattern_row("studio-y.fr", "{f}{last}")
    assert stale.failed_checks == 1 and stale.confidence < 0.7
    good = await pattern_row("studio-y.fr", "{first}")
    assert good.successful_checks == 1 and good.confidence > stale.confidence

    await store.find_and_save_for_person(ws, paul)  # same addresses again → no new outcomes
    assert (await pattern_row("studio-y.fr", "{first}")).successful_checks == 1
    assert (await pattern_row("studio-y.fr", "{f}{last}")).failed_checks == 1


async def test_find_and_save_keeps_user_confirmed_email(workspace, verifier):
    ws, _ = workspace
    cid = await make_company(ws, "agence-x.fr")
    pid = await make_person(ws, cid, "Marie", "Dupont")
    row = await store.save_finding(
        ws, person_id=pid, company_id=cid, finding=finding("md@agence-x.fr", S.RISKY, 0.6)
    )
    async with session_scope() as s:
        await s.execute(sa.update(Email).where(Email.id == row.id).values(is_user_confirmed=True))
    f = await store.find_and_save_for_person(ws, pid)
    assert f.address == "md@agence-x.fr" and f.status == S.RISKY and verifier.calls == []


async def test_find_and_save_without_domain_or_unknown_person(workspace, verifier):
    ws, _ = workspace
    pid = await make_person(ws, None, "Marie", "Dupont")
    f = await store.find_and_save_for_person(ws, pid)
    assert f.address is None and f.reason == "No company domain"
    with pytest.raises(NotFound):
        await store.find_and_save_for_person(ws, uuid.uuid4())
    with pytest.raises(NotFound):
        await store.find_and_save_for_person(uuid.uuid4(), pid)  # other workspace


# ---- jobs -------------------------------------------------------------------------------------


def ctx(ws, type_, payload):
    return JobContext(
        job_id=uuid.uuid4(),
        workspace_id=ws,
        campaign_id=None,
        type=type_,
        payload=payload,
        attempt=1,
        worker_id="test",
    )


async def test_email_jobs(workspace, verifier, monkeypatch):
    """email.find runs the fast path; the ambiguous guess is settled by ONE per-domain SMTP batch (deep path)."""
    from scout.email import engine as eng
    from scout.email.deep import process_domain
    from scout.email.smtp.deep_verifiers import set_deep_verifier
    from scout.email.smtp.health import MemoryHealthStore, SmtpHealthMonitor
    from scout.email.smtp.world import MailWorld, WorldDeepVerifier
    from tests.unit.email.fakes import load_manifest

    ws, _ = workspace
    world = MailWorld.from_manifest(load_manifest())
    deep = WorldDeepVerifier(world)

    async def world_mx(domain: str, *, use_cache: bool = True) -> edns.MxInfo:  # never real DNS in tests
        return await deep.resolve_mx(domain)

    monkeypatch.setattr(edns, "mx_lookup", world_mx)
    monkeypatch.setattr(eng, "_smtp_capable", lambda: True)
    set_deep_verifier(deep)
    try:
        assert get_handler("email.find") is not None and get_handler("email.verify") is not None
        cid = await make_company(ws, "agence-x.fr", employee_max=5000)
        jean = await make_person(ws, cid, "Jean", "Martin")

        res = await email_find(ctx(ws, "email.find", {"person_ids": [str(jean), str(uuid.uuid4())]}))
        assert res["processed"] == 2 and res["missing"] == 1
        assert set(res["statuses"]) <= {
            "RISKY",
            "UNKNOWN",
        }  # no convention known yet: not settled by the fast path
        async with session_scope() as s:
            req = await s.scalar(
                sa.select(EmailVerificationRequest).where(EmailVerificationRequest.person_id == jean)
            )
        assert req is not None and req.domain == "agence-x.fr" and 1 <= len(req.candidates) <= 3

        monitor = SmtpHealthMonitor(MemoryHealthStore(), enabled=lambda: True)
        summary = await process_domain(
            "agence-x.fr", verifier=deep, monitor=monitor, canary=False, schedule=False
        )
        assert summary["claimed"] == 1 and summary["done"] == 1
        assert world.sessions == 1  # one SMTP session for the domain
        row = await get_email(ws, "jean.martin@agence-x.fr")
        assert row is not None and row.status == S.SAFE
        async with session_scope() as s:
            events = (await s.scalars(sa.select(JobEvent).where(JobEvent.workspace_id == ws))).all()
        assert any(
            e.type == "cell.updated" and e.payload.get("value") == "jean.martin@agence-x.fr" for e in events
        )
    finally:
        set_deep_verifier(None)

    row = await get_email(ws, "jean.martin@agence-x.fr")
    assert await email_verify(ctx(ws, "email.verify", {"email_ids": [str(row.id)]})) == {
        "requested": 1,
        "updated": 1,
    }

    from scout.errors import PermanentError

    with pytest.raises(PermanentError):
        await email_find(ctx(ws, "email.find", {"person_ids": "nope"}))
    with pytest.raises(PermanentError):
        await email_verify(ctx(ws, "email.verify", {"email_ids": ["not-a-uuid"]}))
