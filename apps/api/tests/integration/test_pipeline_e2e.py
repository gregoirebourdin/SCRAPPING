"""End-to-end campaign pipeline on the real Postgres job queue (spec §199–§203).

Discovery uses the fixture source; every company website is served by a local FixtureServer
(crawler host overrides, SSRF-safe client exercised for real); AI is the deterministic local
provider; emails are verified by the fixture verifier. The worker runs in-process until every
campaign is terminal.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.enums import (
    CampaignStatus,
    CandidateOutcome,
    EmailStatus,
    EntityType,
    ExclusionMode,
    ExposureType,
    JobStatus,
    ReservationStatus,
    SuppressionReason,
)
from scout.db.models import (
    Campaign,
    CampaignReservation,
    CampaignStats,
    Company,
    CompanyDiscoveryEvent,
    CompanyFieldObservation,
    Email,
    EmailCheck,
    Job,
    LeadExposure,
    ListMembership,
    Person,
    PersonDiscoveryEvent,
    PersonFieldObservation,
    QualificationScore,
)
from scout.pipeline import campaigns as campaigns_svc
from scout.pipeline.icp import parse_prompt
from scout.schemas.campaign import CampaignDefinition
from scout.util.pools import reset_pools
from tests.integration.pipeline_helpers import (
    ALL_HOSTS,
    KREA,
    LUMIERE,
    LUMIERE_STAFF,
    LUMIERE_TESTIMONIALS,
    NOVA,
    OPTOUT,
    PIXEL,
    SPAM,
    build_server,
    drive,
    write_manifest,
)
from tests.unit.crawl.fixture_server import configure_overrides, reset_crawl_state

pytestmark = pytest.mark.integration

PROMPT = "Find 100 French marketing agencies. Founder/CEO only. Do not include leads already seen."


@pytest.fixture
async def env(db, monkeypatch, tmp_path):
    """Local websites + fixture discovery manifest + fixture email verifier + local (no-key) AI."""
    from scout.ai.factory import set_ai
    from scout.config import get_settings
    from scout.email.verifier import set_verifier

    server = build_server()
    await server.start()
    manifest = write_manifest(tmp_path)
    monkeypatch.setenv("DISCOVERY_FIXTURE_MANIFEST", str(manifest))
    monkeypatch.setenv("VERIFIER_BACKEND", "fixture")
    monkeypatch.setenv("AI_PROVIDER", "local")
    configure_overrides(monkeypatch, {h: server.target() for h in [*ALL_HOSTS, f"www.{LUMIERE}"]})
    get_settings.cache_clear()
    set_verifier(None)
    set_ai(None)
    reset_pools()
    try:
        yield server
    finally:
        await server.stop()
        await reset_crawl_state()
        set_verifier(None)
        set_ai(None)
        get_settings.cache_clear()


async def _start(
    ws: uuid.UUID,
    user: str,
    prompt: str = PROMPT,
    *,
    defn: CampaignDefinition | None = None,
    target_list_id: uuid.UUID | None = None,
) -> uuid.UUID:
    if defn is None:
        defn, _ = await parse_prompt(prompt)
    async with session_scope() as s:
        c = await campaigns_svc.create_campaign(
            s, ws, defn, user_id=user, prompt=prompt, target_list_id=target_list_id
        )
        return c.id


async def _campaign(cid: uuid.UUID) -> tuple[Campaign, CampaignStats]:
    async with session_scope() as s:
        c = await s.get(Campaign, cid)
        st = await s.get(CampaignStats, cid)
        assert c is not None and st is not None
        return c, st


async def _delivered(cid: uuid.UUID) -> dict[str, dict[str, Any]]:
    """Leads delivered by this campaign (qualified person event + DISCOVERED exposure + target list membership),
    keyed by full name."""
    async with session_scope() as s:
        c = await s.get(Campaign, cid)
        assert c is not None
        rows = (
            await s.execute(
                sa.select(Person.full_name, Person.id, Company.normalized_domain, Email.address, Email.status)
                .join(PersonDiscoveryEvent, PersonDiscoveryEvent.person_id == Person.id)
                .join(Company, Company.id == Person.company_id)
                .outerjoin(Email, Email.id == Person.primary_email_id)
                .where(
                    PersonDiscoveryEvent.campaign_id == cid,
                    PersonDiscoveryEvent.outcome == CandidateOutcome.qualified,
                )
            )
        ).all()
        listed = set(
            (
                await s.scalars(
                    sa.select(ListMembership.person_id).where(
                        ListMembership.list_id == c.target_list_id, ListMembership.person_id.is_not(None)
                    )
                )
            ).all()
        )
        exposed = set(
            (
                await s.scalars(
                    sa.select(LeadExposure.entity_id).where(
                        LeadExposure.campaign_id == cid,
                        LeadExposure.entity_type == EntityType.person,
                        LeadExposure.exposure_type == ExposureType.DISCOVERED,
                    )
                )
            ).all()
        )
    out = {n: {"id": pid, "domain": d, "email": a, "status": st} for n, pid, d, a, st in rows}
    assert {v["id"] for v in out.values()} == exposed, "every delivered lead has its DISCOVERED exposure"
    assert {v["id"] for v in out.values()} <= listed, "every delivered lead is in the target list"
    return out


async def _company_events(cid: uuid.UUID) -> dict[str, CompanyDiscoveryEvent]:
    async with session_scope() as s:
        rows = (
            await s.scalars(sa.select(CompanyDiscoveryEvent).where(CompanyDiscoveryEvent.campaign_id == cid))
        ).all()
    return {r.name or "": r for r in rows}


async def _suppress_spam_and_optout(ws: uuid.UUID, user: str) -> None:
    from scout.services.suppression import suppress

    async with session_scope() as s:
        await suppress(
            s,
            ws,
            reason=SuppressionReason.opt_out,
            user_id=user,
            domains=[SPAM],
            emails=["Paul.Girard@agence-optout.fr"],
        )


# =============================================================================================
# §200 — the critical campaign flow (backend part), then the identical re-run
# =============================================================================================


async def test_campaign_end_to_end_and_identical_rerun(env, workspace):
    ws, user = workspace
    await _suppress_spam_and_optout(ws, user)

    cid = await _start(ws, user)
    async with session_scope() as s:
        c = await s.get(Campaign, cid)
        assert c is not None and c.status == CampaignStatus.planning
        interp = {i["label"]: i["value"] for i in c.interpretation}
    assert interp["Previously seen"].startswith("People excluded")
    assert "Founder" in interp["People"]

    await drive([cid])
    c, st = await _campaign(cid)

    # ---- campaign stats + explicit stop reason ----
    assert c.status == CampaignStatus.exhausted, (c.status, c.stop_reason)
    assert c.stop_reason and c.stop_reason.startswith("All eligible sources exhausted — 2/100 qualified")
    assert c.stopped_at is not None
    assert st.raw_discovered == 7
    assert st.duplicates == 1  # agence-lumiere.fr reached twice
    assert st.suppressed == 2  # spam-agency.fr (domain) + Paul Girard (email)
    assert st.unique_new_companies == 5
    assert st.companies_evaluated == 5  # every queued company crawled exactly once
    assert st.qualified == 2
    assert st.in_flight == 0
    assert st.qualified + st.rejected == st.unique_new_companies

    # ---- qualified leads in the target list (and only them) ----
    leads = await _delivered(cid)
    assert set(leads) == {"Claire Fontaine", "Antoine Lefort"}
    claire, antoine = leads["Claire Fontaine"], leads["Antoine Lefort"]
    assert claire["email"] == "claire.fontaine@agence-lumiere.fr" and claire["status"] == EmailStatus.SAFE
    assert antoine["email"] == "antoine.lefort@studio-nova.fr" and antoine["status"] == EmailStatus.SAFE

    # ---- rejections are explicit and honest ----
    events = await _company_events(cid)
    # the in-campaign duplicate is dropped by the (campaign, candidate_key) unique key: counted, never re-processed
    assert "Agence Lumière (bureau de Lyon)" not in events
    assert events["Spam Agency"].outcome == CandidateOutcome.suppressed
    assert events["Kréa Com"].outcome == CandidateOutcome.rejected
    assert "Email status CATCH_ALL not accepted" in (events["Kréa Com"].reason or "")
    assert events["Pixel Factory"].outcome == CandidateOutcome.rejected
    assert events["Pixel Factory"].reason == "No decision maker found"
    assert events["Agence Optout"].outcome == CandidateOutcome.rejected
    assert "suppressed" in (events["Agence Optout"].reason or "").lower()
    assert env.count(SPAM) == 0  # suppressed before any crawl
    assert env.count(f"www.{LUMIERE}") == 0  # duplicate never crawled

    async with session_scope() as s:
        # ---- global registry: one company per domain ----
        domains = (
            await s.scalars(sa.select(Company.normalized_domain).where(Company.workspace_id == ws))
        ).all()
        # (the suppressed domain is never registered: suppression is checked before the registry upsert)
        assert sorted(d for d in domains if d) == sorted([LUMIERE, NOVA, KREA, PIXEL, OPTOUT])
        lumiere = await s.scalar(
            sa.select(Company).where(Company.workspace_id == ws, Company.normalized_domain == LUMIERE)
        )
        assert lumiere is not None
        assert (
            lumiere.registry_id == "812345676" and lumiere.registry_source == "fr_sirene"
        )  # from the legal notice
        assert lumiere.phone and lumiere.linkedin_url == "https://www.linkedin.com/company/agence-lumiere/"
        assert lumiere.website_status.value == "ok" and lumiere.last_crawled_at is not None

        # ---- people: only real staff, never testimonial authors or company names ----
        people = (await s.scalars(sa.select(Person).where(Person.workspace_id == ws))).all()
        names = {p.full_name for p in people}
        assert not names & LUMIERE_TESTIMONIALS
        assert "Agence Lumière" not in names and "Studio Nova" not in names
        assert {p.full_name for p in people if p.company_id == lumiere.id} <= LUMIERE_STAFF
        assert "Bruno Caron" not in names and "Léa Morel" not in names  # not decision makers → never stored

        # ---- honest email statuses ----
        emails = {
            e.address: e for e in (await s.scalars(sa.select(Email).where(Email.workspace_id == ws))).all()
        }
        krea = [e for e in emails.values() if e.domain == KREA]
        assert krea and all(e.status == EmailStatus.CATCH_ALL for e in krea)
        assert emails["claire.fontaine@agence-lumiere.fr"].discovery_method.value == "published"
        assert emails["antoine.lefort@studio-nova.fr"].discovery_method.value in (
            "permutation",
            "inferred_pattern",
        )
        checks = (await s.scalars(sa.select(EmailCheck).where(EmailCheck.workspace_id == ws))).all()
        assert {ch.verifier for ch in checks} == {"fixture"}

        # ---- ICP scores + quality gate ----
        scores = (
            await s.scalars(sa.select(QualificationScore).where(QualificationScore.campaign_id == cid))
        ).all()
        by_person = {sc.person_id: sc for sc in scores if sc.person_id}
        for lead in leads.values():
            sc = by_person[lead["id"]]
            assert sc.qualified and sc.icp_score >= 75
            assert all(g["passed"] for g in sc.gate_results)
        rejected_scores = [sc for sc in scores if not sc.qualified]
        assert rejected_scores, "rejected candidates keep their score + failing gate"
        for sc in rejected_scores:
            assert any(not g["passed"] for g in sc.gate_results)

        # ---- provenance: company + person field observations with source URLs and evidence ----
        obs = (
            await s.scalars(
                sa.select(CompanyFieldObservation).where(CompanyFieldObservation.company_id == lumiere.id)
            )
        ).all()
        fields = {o.field_name for o in obs}
        assert {
            "name",
            "website_url",
            "registry_id",
            "phone",
            "address",
            "description",
            "linkedin_url",
        } <= fields
        website_obs = [o for o in obs if o.source_key == "website"]
        assert website_obs and all(o.source_url and o.evidence for o in website_obs)
        assert all(o.page_id for o in website_obs), "website facts link to the cached page they were read on"
        claire_obs = (
            await s.scalars(
                sa.select(PersonFieldObservation).where(PersonFieldObservation.person_id == claire["id"])
            )
        ).all()
        assert {o.field_name for o in claire_obs} >= {"full_name", "job_title"}
        assert all(o.source_url and o.evidence for o in claire_obs)

        # ---- exposures: DISCOVERED + ADDED_TO_LIST for the person and the company ----
        expo = (
            await s.execute(
                sa.select(LeadExposure.entity_type, LeadExposure.exposure_type, sa.func.count())
                .where(LeadExposure.workspace_id == ws, LeadExposure.campaign_id == cid)
                .group_by(LeadExposure.entity_type, LeadExposure.exposure_type)
            )
        ).all()
        counts = {(et.value, xt.value): n for et, xt, n in expo}
        assert counts[("person", "DISCOVERED")] == 2 and counts[("company", "DISCOVERED")] == 2
        assert counts[("person", "ADDED_TO_LIST")] == 2

        # reservations converted, none left dangling
        res = (
            await s.scalars(sa.select(CampaignReservation).where(CampaignReservation.campaign_id == cid))
        ).all()
        assert {r.status for r in res} <= {ReservationStatus.qualified, ReservationStatus.released}
        assert sum(1 for r in res if r.status == ReservationStatus.qualified) == 2

    # ---- the cached pages (DB rows) feed the extractors: 5 staff, no testimonial authors ----
    from scout.crawl.cache import get_cached_pages
    from scout.extract.people import extract_people

    pages = await get_cached_pages(ws, lumiere.id)
    extracted = {p.full_name for p in extract_people(pages, company_name="Agence Lumière", domain=LUMIERE)}
    assert extracted == LUMIERE_STAFF

    # §203: removing a contact from a list never erases global history
    from scout.services import lists as lists_svc

    async with session_scope() as s:
        await lists_svc.remove_from_list(s, ws, c.target_list_id, EntityType.person, [claire["id"]])

    # =========================================================================================
    # identical re-run: previously seen people are excluded, nothing is duplicated
    # =========================================================================================
    requests_before = {h: env.count(h) for h in ALL_HOSTS}
    async with session_scope() as s:
        n_companies = await s.scalar(
            sa.select(sa.func.count()).select_from(Company).where(Company.workspace_id == ws)
        )
    cid2 = await _start(ws, user)
    await drive([cid2])
    c2, st2 = await _campaign(cid2)
    leads2 = await _delivered(cid2)
    assert c2.status == CampaignStatus.exhausted
    assert not set(leads2) & {"Claire Fontaine", "Antoine Lefort"}, leads2
    assert set(leads2) == {"Julien Moreau"}  # a *new* founder at a known company is allowed
    assert st2.excluded_previous >= 2
    assert {h: env.count(h) for h in ALL_HOSTS} == requests_before  # cached crawls reused, zero refetch
    async with session_scope() as s:
        assert (
            await s.scalar(sa.select(sa.func.count()).select_from(Company).where(Company.workspace_id == ws))
            == n_companies
        )
        dupes = (
            await s.execute(
                sa.select(Person.company_id, Person.normalized_name, sa.func.count())
                .where(Person.workspace_id == ws)
                .group_by(Person.company_id, Person.normalized_name)
                .having(sa.func.count() > 1)
            )
        ).all()
        assert dupes == []
        reasons = (
            await s.scalars(
                sa.select(PersonDiscoveryEvent.reason).where(
                    PersonDiscoveryEvent.campaign_id == cid2,
                    PersonDiscoveryEvent.outcome == CandidateOutcome.excluded_previous,
                )
            )
        ).all()
        assert reasons and all("previously seen" in (r or "") for r in reasons)
        # every person was delivered at most once across both campaigns
        per_person = (
            await s.execute(
                sa.select(LeadExposure.entity_id, sa.func.count())
                .where(
                    LeadExposure.workspace_id == ws,
                    LeadExposure.entity_type == EntityType.person,
                    LeadExposure.exposure_type == ExposureType.DISCOVERED,
                )
                .group_by(LeadExposure.entity_id)
            )
        ).all()
        assert all(n == 1 for _, n in per_person)


# =============================================================================================
# Campaign resume (spec §199): cooperative pause in the middle of a company, queue pause, resume
# =============================================================================================


async def test_pause_and_resume_running_campaign(env, workspace, monkeypatch):
    from scout.crawl import cache
    from scout.jobs.worker import Worker
    from scout.main import load_handlers

    ws, user = workspace
    await _suppress_spam_and_optout(ws, user)
    cid = await _start(ws, user)
    load_handlers()

    real_ensure = cache.ensure_crawled
    paused_during: list[uuid.UUID] = []

    async def pausing_ensure(workspace_id, company_id, **kw):
        pages = await real_ensure(workspace_id, company_id, **kw)
        if not paused_during:  # the user hits "Pause" while the first company is being crawled
            paused_during.append(company_id)
            async with session_scope() as s:
                await campaigns_svc.pause_campaign(s, ws, cid)
        return pages

    monkeypatch.setattr(cache, "ensure_crawled", pausing_ensure)
    # Discovery + the first company stages run; the in-flight handlers stop at their next checkpoint.
    await Worker(slots=1).run_until_idle(timeout_s=30, idle_rounds=3)

    c, st = await _campaign(cid)
    assert c.status == CampaignStatus.paused and c.paused_at is not None
    assert st.qualified == 0
    async with session_scope() as s:
        jobs = (await s.scalars(sa.select(Job).where(Job.campaign_id == cid))).all()
        process = [j for j in jobs if j.type == "company.process"]
        assert process and all(j.status == JobStatus.paused for j in process)
        assert all(j.attempts == 0 for j in process), "a cooperative pause never consumes an attempt"
        ev = await s.scalar(
            sa.select(CompanyDiscoveryEvent).where(
                CompanyDiscoveryEvent.company_id == paused_during[0], CompanyDiscoveryEvent.campaign_id == cid
            )
        )
        assert ev is not None and ev.outcome == CandidateOutcome.pending
        assert (
            ev.stage == "crawl" and ev.stage_data.get("website_done") and ev.stage_data.get("crawl_counted")
        )
    requests_while_paused = env.count()

    # A paused campaign makes no progress, whatever the workers do.
    await Worker(slots=4).run_until_idle(timeout_s=10, idle_rounds=2)
    assert env.count() == requests_while_paused
    _, st = await _campaign(cid)
    assert st.qualified == 0 and st.companies_evaluated == 1

    async with session_scope() as s:
        await campaigns_svc.resume_campaign(s, ws, cid)
    await drive([cid])
    c, st = await _campaign(cid)
    assert c.status == CampaignStatus.exhausted
    assert set(await _delivered(cid)) == {"Claire Fontaine", "Antoine Lefort"}
    assert st.qualified == 2
    assert st.companies_evaluated == 5, "resumed work is not counted twice"
    # the company crawled before the pause was not fetched again after resume (checkpointed + cached)
    async with session_scope() as s:
        comp = await s.get(Company, paused_during[0])
        assert comp is not None
        runs = await s.scalar(
            sa.text("SELECT count(*) FROM website_crawl_runs WHERE company_id = :c"), {"c": comp.id}
        )
    assert runs == 1


# =============================================================================================
# Atomic reservation: two concurrent campaigns in one workspace never take the same lead
# =============================================================================================


async def test_concurrent_campaigns_never_deliver_the_same_lead(env, workspace, monkeypatch, tmp_path):
    from scout.services import registry

    ws, user = workspace
    manifest = write_manifest(
        tmp_path,
        [
            {
                "name": "Agence Lumière",
                "website": f"https://{LUMIERE}",
                "city": "Paris",
                "country": "FR",
                "category": "Agence marketing",
            },
        ],
    )
    monkeypatch.setenv("DISCOVERY_FIXTURE_MANIFEST", str(manifest))
    from scout.config import get_settings

    get_settings.cache_clear()

    # Both campaigns pass the exclusion checks for the same person at the same moment: only the atomic
    # reservation can arbitrate.
    barrier = asyncio.Barrier(2)
    seen: set[uuid.UUID] = set()
    real_upsert = registry.upsert_person

    async def synchronized_upsert(s, workspace_id, **kw):
        cid = kw.get("campaign_id")
        if cid is not None and cid not in seen:
            seen.add(cid)
            async with asyncio.timeout(30):
                await barrier.wait()
        return await real_upsert(s, workspace_id, **kw)

    monkeypatch.setattr(registry, "upsert_person", synchronized_upsert)
    a = await _start(ws, user, "Find French marketing agencies. Founder/CEO only. Only new leads.")
    b = await _start(ws, user, "Find French marketing agencies. Founder/CEO only. Only new leads.")
    await drive([a, b])

    leads_a, leads_b = await _delivered(a), await _delivered(b)
    assert not set(leads_a) & set(leads_b), (leads_a, leads_b)
    assert "Claire Fontaine" in set(leads_a) | set(leads_b)
    _, st_a = await _campaign(a)
    _, st_b = await _campaign(b)
    assert st_a.reserved_elsewhere + st_b.reserved_elsewhere == 1
    async with session_scope() as s:
        loser = await s.scalar(
            sa.select(PersonDiscoveryEvent).where(
                PersonDiscoveryEvent.outcome == CandidateOutcome.reserved_elsewhere
            )
        )
        assert loser is not None and loser.campaign_id in (a, b)
        claire = await s.scalar(
            sa.select(Person).where(Person.workspace_id == ws, Person.full_name == "Claire Fontaine")
        )
        assert claire is not None and loser.person_id == claire.id
        # one person row, one DISCOVERED exposure, no reservation left behind
        assert (
            await s.scalar(
                sa.select(sa.func.count())
                .select_from(Person)
                .where(Person.workspace_id == ws, Person.full_name == "Claire Fontaine")
            )
            == 1
        )
        assert (
            await s.scalar(
                sa.select(sa.func.count())
                .select_from(LeadExposure)
                .where(
                    LeadExposure.entity_id == claire.id, LeadExposure.exposure_type == ExposureType.DISCOVERED
                )
            )
            == 1
        )
        assert (
            await s.scalar(
                sa.select(sa.func.count())
                .select_from(CampaignReservation)
                .where(
                    CampaignReservation.workspace_id == ws,
                    CampaignReservation.status == ReservationStatus.reserved,
                )
            )
            == 0
        )
    # one crawl for the shared domain, reused by the second campaign
    assert env.count(LUMIERE) > 0
    async with session_scope() as s:
        runs = await s.scalar(
            sa.text("SELECT count(*) FROM website_crawl_runs WHERE workspace_id = :w"), {"w": ws}
        )
    assert runs == 1


# =============================================================================================
# Target reached → completed, remaining work cancelled (never left paused)
# =============================================================================================


async def test_target_reached_completes_and_cancels_remaining_work(env, workspace):
    ws, user = workspace
    defn, _ = await parse_prompt(PROMPT)
    defn.target_qualified_count = 1
    cid = await _start(ws, user, defn=defn)
    await drive([cid])
    c, st = await _campaign(cid)
    assert c.status == CampaignStatus.completed
    assert c.stop_reason == f"Target reached: {st.qualified:,} qualified leads"
    assert st.qualified >= 1
    async with session_scope() as s:
        left = (
            await s.scalars(
                sa.select(Job.status).where(
                    Job.campaign_id == cid,
                    Job.status.in_(
                        [
                            JobStatus.pending,
                            JobStatus.retrying,
                            JobStatus.paused,
                            JobStatus.claimed,
                            JobStatus.running,
                        ]
                    ),
                )
            )
        ).all()
    assert left == []


# =============================================================================================
# §201 — a different decision maker at the companies of a list
# =============================================================================================


async def test_different_decision_maker_at_list_companies(env, workspace):
    from scout.pipeline.icp import ParseContext

    ws, user = workspace
    await _suppress_spam_and_optout(ws, user)
    first = await _start(ws, user)
    await drive([first])
    c1, _ = await _campaign(first)
    assert set(await _delivered(first)) == {"Claire Fontaine", "Antoine Lefort"}
    async with session_scope() as s:
        n_companies = await s.scalar(
            sa.select(sa.func.count()).select_from(Company).where(Company.workspace_id == ws)
        )

    prompt = "Find a different marketing decision maker at all companies in this list"
    defn, _ = await parse_prompt(
        prompt, ParseContext(current_list_id=c1.target_list_id, current_list_name=c1.name)
    )
    assert defn.seed.type == "list" and defn.exclusion.mode == ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE
    second = await _start(ws, user, prompt, defn=defn)
    await drive([second])
    c2, st2 = await _campaign(second)
    leads = await _delivered(second)
    assert "Julie Bernard" in leads  # Head of Marketing equivalent at Studio Nova
    assert leads["Julie Bernard"]["status"] == EmailStatus.SAFE
    assert not set(leads) & {"Claire Fontaine", "Antoine Lefort"}
    assert c2.status in (CampaignStatus.exhausted, CampaignStatus.completed)
    assert st2.raw_discovered == 2  # the two companies of the list, nothing rediscovered
    async with session_scope() as s:
        # the companies remain eligible and are never duplicated
        assert (
            await s.scalar(sa.select(sa.func.count()).select_from(Company).where(Company.workspace_id == ws))
            == n_companies
        )
        nova = await s.scalar(
            sa.select(CompanyDiscoveryEvent).where(
                CompanyDiscoveryEvent.campaign_id == second, CompanyDiscoveryEvent.domain == NOVA
            )
        )
        assert nova is not None and nova.outcome == CandidateOutcome.qualified


# =============================================================================================
# §202 — imported contacts are excluded from a "new contacts" campaign
# =============================================================================================


async def test_imported_contacts_are_excluded(env, workspace):
    from scout.jobs.worker import Worker
    from scout.main import load_handlers
    from scout.pipeline.icp import ParseContext
    from scout.services import imports

    ws, user = workspace
    load_handlers()
    csv = (
        "Name,Email,Company,Website,Title\n"
        "Claire Fontaine,claire.fontaine@agence-lumiere.fr,Agence Lumière,agence-lumiere.fr,CEO\n"
        "Antoine Lefort,antoine.lefort@studio-nova.fr,Studio Nova,studio-nova.fr,Founder\n"
    ).encode()
    async with session_scope() as s:
        imp = await imports.start_import(
            s,
            ws,
            raw=csv,
            filename="contacts.csv",
            list_id=None,
            mark_as_known=True,
            user_id=user,
            mapping={
                "Name": "full_name",
                "Email": "email",
                "Company": "company",
                "Website": "website",
                "Title": "title",
            },
        )
        import_id = imp.id
    await Worker(slots=2).run_until_idle(timeout_s=20, idle_rounds=2)

    prompt = (
        "Find 1,000 new French marketing agency founders, but do not give me anyone from my uploaded file"
    )
    defn, _ = await parse_prompt(prompt, ParseContext(latest_import_id=import_id))
    assert import_id in defn.exclusion.import_ids
    cid = await _start(ws, user, prompt, defn=defn)
    await drive([cid])
    leads = await _delivered(cid)
    assert not set(leads) & {"Claire Fontaine", "Antoine Lefort"}
    assert "Julien Moreau" in leads  # new founder at a company that was imported
    async with session_scope() as s:
        # the import created the canonical companies; the campaign reused them (registry dedupe by domain)
        domains = (
            await s.scalars(sa.select(Company.normalized_domain).where(Company.workspace_id == ws))
        ).all()
        assert len(domains) == len(set(domains))
        people = (await s.scalars(sa.select(Person.full_name).where(Person.workspace_id == ws))).all()
        assert len(people) == len(set(people))


# =============================================================================================
# Registry candidates without a website: resolution, identity proof, registry dedupe
# =============================================================================================


async def test_registry_candidate_without_website_is_resolved_and_deduplicated(
    env, workspace, monkeypatch, tmp_path
):
    from scout.config import get_settings
    from scout.crawl import resolve

    async def dns_only_overrides(domain: str) -> bool:  # never touch real DNS in tests
        return domain in get_settings().crawler_host_overrides

    monkeypatch.setattr(resolve, "dns_exists", dns_only_overrides)
    ws, user = workspace
    manifest = write_manifest(
        tmp_path,
        [
            {
                "name": "AGENCE LUMIERE",
                "registry_id": "812345676",
                "city": "Paris",
                "country": "FR",
                "category": "Agence marketing",
                "employees": "2-10",
            },
        ],
    )
    monkeypatch.setenv("DISCOVERY_FIXTURE_MANIFEST", str(manifest))
    get_settings.cache_clear()

    cid = await _start(ws, user)
    await drive([cid])
    assert set(await _delivered(cid)) == {"Claire Fontaine"}
    async with session_scope() as s:
        comp = await s.scalar(sa.select(Company).where(Company.workspace_id == ws))
        assert comp is not None
        assert comp.normalized_domain == LUMIERE and comp.website_url
        assert (comp.registry_source, comp.registry_id) == ("fr_sirene", "812345676")
        obs = (
            await s.scalars(
                sa.select(CompanyFieldObservation).where(
                    CompanyFieldObservation.company_id == comp.id,
                    CompanyFieldObservation.field_name == "website_url",
                    CompanyFieldObservation.source_key == "website",
                )
            )
        ).all()
        assert obs and "812345676" in (obs[0].evidence or "").replace(
            " ", ""
        )  # SIREN proof on the legal notice

    # The same company later found by a web source (with a website) is matched, never duplicated.
    manifest2 = write_manifest(
        tmp_path,
        [
            {
                "name": "Agence Lumière",
                "website": f"https://www.{LUMIERE}/",
                "city": "Paris",
                "country": "FR",
                "category": "Agence marketing",
            },
        ],
    )
    monkeypatch.setenv("DISCOVERY_FIXTURE_MANIFEST", str(manifest2))
    get_settings.cache_clear()
    cid2 = await _start(ws, user)
    await drive([cid2])
    async with session_scope() as s:
        assert (
            await s.scalar(sa.select(sa.func.count()).select_from(Company).where(Company.workspace_id == ws))
            == 1
        )
    assert set(await _delivered(cid2)) == {"Julien Moreau"}


# =============================================================================================
# Company-only campaigns + "exclude previously exported"
# =============================================================================================


async def test_company_mode_campaign_and_exclude_exported_rerun(env, workspace):
    from scout.db.enums import ExportScope
    from scout.services.exports import export_rows

    ws, user = workspace
    prompt = "Find French marketing agencies, companies only"
    cid = await _start(ws, user, prompt)
    await drive([cid])
    c, st = await _campaign(cid)
    assert c.status == CampaignStatus.exhausted
    async with session_scope() as s:
        delivered = set(
            (
                await s.scalars(
                    sa.select(Company.normalized_domain)
                    .join(ListMembership, ListMembership.company_id == Company.id)
                    .where(ListMembership.list_id == c.target_list_id)
                )
            ).all()
        )
        scores = (
            await s.scalars(sa.select(QualificationScore).where(QualificationScore.campaign_id == cid))
        ).all()
    assert LUMIERE in delivered and NOVA in delivered and SPAM in delivered
    assert st.qualified == len(delivered)
    assert all(sc.person_id is None for sc in scores) and len(scores) == st.qualified + st.rejected

    async with session_scope() as s:
        exported = await export_rows(
            s,
            ws,
            user_id=user,
            entity_type=EntityType.company,
            scope=ExportScope.list,
            list_id=c.target_list_id,
        )
    assert exported.row_count == len(delivered)

    defn, _ = await parse_prompt(prompt)
    defn.exclusion.mode = ExclusionMode.EXCLUDE_EXPORTED
    defn.exclusion.previous_companies = True
    cid2 = await _start(ws, user, prompt, defn=defn)
    await drive([cid2])
    c2, st2 = await _campaign(cid2)
    async with session_scope() as s:
        again = (
            await s.scalars(
                sa.select(ListMembership.company_id).where(ListMembership.list_id == c2.target_list_id)
            )
        ).all()
    assert again == []
    assert st2.excluded_previous == len(delivered)


async def test_concurrent_company_mode_campaigns_take_each_company_once(
    env, workspace, monkeypatch, tmp_path
):
    from scout.config import get_settings
    from scout.pipeline import processor

    ws, user = workspace
    manifest = write_manifest(
        tmp_path,
        [
            {
                "name": "Agence Lumière",
                "website": f"https://{LUMIERE}",
                "city": "Paris",
                "country": "FR",
                "category": "Agence marketing",
            },
        ],
    )
    monkeypatch.setenv("DISCOVERY_FIXTURE_MANIFEST", str(manifest))
    get_settings.cache_clear()

    barrier = asyncio.Barrier(2)
    real_expire = processor._expire_stale_reservation

    async def synchronized(s, workspace_id, entity_type, key):
        if entity_type == EntityType.company:
            async with asyncio.timeout(30):
                await barrier.wait()  # both campaigns try to hold the same company at the same moment
        return await real_expire(s, workspace_id, entity_type, key)

    monkeypatch.setattr(processor, "_expire_stale_reservation", synchronized)
    prompt = "Find French marketing agencies, companies only"
    a, b = await _start(ws, user, prompt), await _start(ws, user, prompt)
    await drive([a, b])
    async with session_scope() as s:
        lists = []
        for cid in (a, b):
            c = await s.get(Campaign, cid)
            assert c is not None
            lists.append(
                set(
                    (
                        await s.scalars(
                            sa.select(ListMembership.company_id).where(
                                ListMembership.list_id == c.target_list_id
                            )
                        )
                    ).all()
                )
            )
    assert len(lists[0]) + len(lists[1]) == 1, lists
    _, st_a = await _campaign(a)
    _, st_b = await _campaign(b)
    assert st_a.qualified + st_b.qualified == 1
    assert st_a.reserved_elsewhere + st_b.reserved_elsewhere == 1


# =============================================================================================
# §200 — the operator part: list, semantic column, filter, add to list, keyword column on cached sites,
# SAFE filter, export, "start the same campaign again" — all through the chat operator (local router)
# =============================================================================================


async def _say(
    ws: uuid.UUID, user: str, thread_id: uuid.UUID | None, ui, text: str
) -> tuple[uuid.UUID, list]:
    from scout.auth.context import WorkspaceContext
    from scout.chat.operator import get_or_create_thread, run_turn
    from scout.db.enums import MemberRole

    wctx = WorkspaceContext(workspace_id=ws, user_id=user, role=MemberRole.owner)
    thread = await get_or_create_thread(wctx, thread_id, ui.list_id)
    events = [ev async for ev in run_turn(wctx, thread, text, ui)]
    errors = [d for e, d in events if e == "error"]
    assert not errors, errors
    failed = [d for e, d in events if e == "tool_result" and d["status"] != "executed"]
    assert not failed, failed
    return thread.id, events


async def test_critical_scenario_200_through_the_chat_operator(env, workspace):
    import httpx

    from scout.ai.factory import FakeProvider, set_ai
    from scout.auth.context import issue_service_token
    from scout.chat.context import UIContext
    from scout.db.enums import CellStatus
    from scout.db.models import CustomColumn, CustomFieldValue, List
    from scout.jobs.worker import Worker
    from scout.main import create_app

    ws, user = workspace
    await _suppress_spam_and_optout(ws, user)
    cid = await _start(ws, user)
    await drive([cid])
    c, _ = await _campaign(cid)
    assert set(await _delivered(cid)) == {"Claire Fontaine", "Antoine Lefort"}

    # The semantic classifier needs a model: a scripted one that must quote the website verbatim.
    fake = FakeProvider()

    def verdict(prompt: str) -> dict:
        if LUMIERE in prompt:
            return {
                "verdict": "true",
                "confidence": 0.9,
                "evidence_quote": "Stratégie éditoriale, création de contenus, community management et campagnes Meta Ads.",
            }
        return {
            "verdict": "false",
            "confidence": 0.85,
            "evidence_quote": "Stratégie marketing, réseaux sociaux, publicité en ligne et création de contenus.",
        }

    fake.on("SemanticVerdict", verdict)
    set_ai(fake)
    ui = UIContext(
        list_id=c.target_list_id,
        list_name=c.name,
        scope="list",
        entity_type=EntityType.person,
        visible_columns=["full_name", "email", "email_status", "company"],
    )
    enrich = Worker(slots=4, types=["enrichment.batch", "enrichment.column"])

    thread, _ = await _say(ws, user, None, ui, "Create a list called Instagram Agencies.")
    async with session_scope() as s:
        target = await s.scalar(
            sa.select(List).where(List.workspace_id == ws, List.name == "Instagram Agencies")
        )
        assert target is not None

    await _say(
        ws,
        user,
        thread,
        ui,
        "Add a column called Offers Instagram and determine whether they actually sell Instagram management.",
    )
    await enrich.run_until_idle(timeout_s=60)
    async with session_scope() as s:
        col = await s.scalar(
            sa.select(CustomColumn).where(
                CustomColumn.workspace_id == ws, CustomColumn.name == "Offers Instagram"
            )
        )
        assert col is not None and col.configuration["strategy"] == "semantic_classifier"
        cells = {
            v.entity_id: v
            for v in (
                await s.scalars(sa.select(CustomFieldValue).where(CustomFieldValue.column_id == col.id))
            ).all()
        }
        lumiere = await s.scalar(
            sa.select(Company.id).where(Company.workspace_id == ws, Company.normalized_domain == LUMIERE)
        )
    assert {v.status for v in cells.values()} == {CellStatus.success}
    assert cells[lumiere].value_json is True and "community management" in (cells[lumiere].evidence or "")
    assert sum(1 for v in cells.values() if v.value_json is False) == 1

    ui.visible_columns.append("Offers Instagram")
    _, events = await _say(ws, user, thread, ui, "Filter Offers Instagram TRUE")
    assert any(e == "ui_effect" and d["type"] == "set_filters" for e, d in events) and ui.filters
    await _say(ws, user, thread, ui, "Put those into Instagram Agencies.")
    async with session_scope() as s:
        members = (
            await s.scalars(
                sa.select(Person.full_name)
                .join(ListMembership, ListMembership.person_id == Person.id)
                .where(ListMembership.list_id == target.id)
            )
        ).all()
    assert members == ["Claire Fontaine"]

    requests_before = env.count()
    await _say(ws, user, thread, ui, "Add whether they mention ManyChat.")
    await enrich.run_until_idle(timeout_s=60)
    assert env.count() == requests_before, "cached sites are reused: no refetch for a new column"
    async with session_scope() as s:
        manychat = await s.scalar(
            sa.select(CustomColumn).where(
                CustomColumn.workspace_id == ws, CustomColumn.name == "Mentions ManyChat"
            )
        )
        assert manychat is not None and manychat.configuration["strategy"] == "keyword"
        mc_values = (
            await s.scalars(
                sa.select(CustomFieldValue.value_json).where(CustomFieldValue.column_id == manychat.id)
            )
        ).all()
    assert mc_values and all(v is False for v in mc_values)

    ui.visible_columns.append("Mentions ManyChat")
    await _say(ws, user, thread, ui, "Only keep SAFE emails.")
    _, events = await _say(ws, user, thread, ui, "Export")
    request = next(d["request"] for e, d in events if e == "ui_effect" and d["type"] == "download_export")
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://scout.test"
    ) as client:
        r = await client.post(
            "/v1/exports",
            json=request,
            headers={
                "Authorization": f"Bearer {issue_service_token(user, email=f'{user}@example.com')}",
                "X-Workspace-Id": str(ws),
            },
        )
    assert r.status_code == 200, r.text
    exported = r.content.decode("utf-8-sig")
    assert "Claire Fontaine" in exported and "Antoine Lefort" not in exported and "SAFE" in exported
    assert r.headers["X-Row-Count"] == "1"

    set_ai(None)  # the campaign itself runs with the deterministic local provider
    _, events = await _say(ws, user, thread, ui, "Start same campaign again")
    card = next(d["card"] for e, d in events if e == "tool_result" and d["tool"] == "rerun_campaign")
    rerun = uuid.UUID(card["campaign_id"])
    await drive([rerun])
    again = await _delivered(rerun)
    assert not set(again) & {"Claire Fontaine", "Antoine Lefort"}, again
    async with session_scope() as s:
        claire_exports = await s.scalar(
            sa.select(sa.func.count())
            .select_from(LeadExposure)
            .join(Person, Person.id == LeadExposure.entity_id)
            .where(Person.full_name == "Claire Fontaine", LeadExposure.exposure_type == ExposureType.EXPORTED)
        )
    assert claire_exports == 1
