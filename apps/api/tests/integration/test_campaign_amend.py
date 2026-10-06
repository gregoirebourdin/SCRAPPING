"""Stop without losing anything, resume, and "resume with changes" (amend) on the real Postgres job queue.

Fixture discovery + local websites + fixture verifier + local AI (see pipeline_helpers). Covers: pause keeps
delivered leads, checkpointed candidates and the discovery frontier; amend previews a diff, updates the
definition / normalized criteria / sources, is audited and emitted; resume continues without double counting
or double delivery; stopped campaigns reopen only once the stop condition is lifted; lifecycle calls are
idempotent; a pause during planning is never overridden; live snapshot + "retry" for stalls.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.enums import CampaignStatus, CandidateOutcome, JobStatus, SuppressionReason
from scout.db.models import (
    AuditLog,
    Campaign,
    CampaignFilter,
    CampaignSource,
    CampaignStats,
    Company,
    CompanyDiscoveryEvent,
    Job,
    JobEvent,
    ListMembership,
    Person,
    PersonDiscoveryEvent,
)
from scout.errors import Conflict
from scout.pipeline import campaigns as csvc
from scout.pipeline.icp import parse_prompt
from scout.schemas.campaign import CampaignAmendment
from scout.util.pools import reset_pools
from tests.integration.pipeline_helpers import ALL_HOSTS, LUMIERE, SPAM, build_server, drive, write_manifest
from tests.unit.crawl.fixture_server import configure_overrides, reset_crawl_state

pytestmark = pytest.mark.integration

PROMPT = "Find 100 French marketing agencies. Founder/CEO only. Do not include leads already seen."


@pytest.fixture
async def env(db, monkeypatch, tmp_path):
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


async def _start(ws: uuid.UUID, user: str, prompt: str = PROMPT) -> uuid.UUID:
    from scout.services.suppression import suppress

    async with session_scope() as s:
        await suppress(
            s,
            ws,
            reason=SuppressionReason.opt_out,
            user_id=user,
            domains=[SPAM],
            emails=["paul.girard@agence-optout.fr"],
        )
    defn, _ = await parse_prompt(prompt)
    async with session_scope() as s:
        c = await csvc.create_campaign(s, ws, defn, user_id=user, prompt=prompt)
        return c.id


async def _state(cid: uuid.UUID) -> tuple[Campaign, CampaignStats]:
    async with session_scope() as s:
        c = await s.get(Campaign, cid)
        st = await s.get(CampaignStats, cid)
        assert c is not None and st is not None
        return c, st


async def _delivered(cid: uuid.UUID) -> list[str]:
    async with session_scope() as s:
        return sorted(
            (
                await s.scalars(
                    sa.select(Person.full_name)
                    .join(PersonDiscoveryEvent, PersonDiscoveryEvent.person_id == Person.id)
                    .where(
                        PersonDiscoveryEvent.campaign_id == cid,
                        PersonDiscoveryEvent.outcome == CandidateOutcome.qualified,
                    )
                )
            ).all()
        )


async def _list_size(cid: uuid.UUID) -> int:
    c, _ = await _state(cid)
    async with session_scope() as s:
        return int(
            await s.scalar(
                sa.select(sa.func.count())
                .select_from(ListMembership)
                .where(ListMembership.list_id == c.target_list_id)
            )
            or 0
        )


async def _events(cid: uuid.UUID, type_: str) -> list[dict[str, Any]]:
    async with session_scope() as s:
        return list(
            (
                await s.scalars(
                    sa.select(JobEvent.payload)
                    .where(JobEvent.campaign_id == cid, JobEvent.type == type_)
                    .order_by(JobEvent.id)
                )
            ).all()
        )


async def test_pause_mid_company_amend_and_resume_keeps_everything(env, workspace, monkeypatch):
    from scout.crawl import cache
    from scout.jobs.worker import Worker
    from scout.main import load_handlers

    ws, user = workspace
    cid = await _start(ws, user)
    load_handlers()
    real_ensure = cache.ensure_crawled
    paused: list[uuid.UUID] = []

    async def pausing_ensure(workspace_id, company_id, **kw):
        pages = await real_ensure(workspace_id, company_id, **kw)
        if not paused:  # "Pause" while the first company is being crawled
            paused.append(company_id)
            async with session_scope() as s:
                await csvc.pause_campaign(s, ws, cid)
        return pages

    monkeypatch.setattr(cache, "ensure_crawled", pausing_ensure)
    await Worker(slots=1).run_until_idle(timeout_s=30, idle_rounds=3)
    monkeypatch.setattr(cache, "ensure_crawled", real_ensure)
    c, st = await _state(cid)
    assert c.status == CampaignStatus.paused
    async with session_scope() as s:
        cursor_before = {
            x.source_key: dict(x.cursor)
            for x in (
                await s.scalars(sa.select(CampaignSource).where(CampaignSource.campaign_id == cid))
            ).all()
        }
    assert cursor_before, "the discovery frontier is kept while paused"
    # double click on Pause: idempotent
    async with session_scope() as s:
        assert (await csvc.pause_campaign(s, ws, cid)).status == CampaignStatus.paused

    # preview: nothing changes in the database
    async with session_scope() as s:
        preview = await csvc.amend_campaign(
            s,
            ws,
            cid,
            CampaignAmendment(instruction="+50 leads et seulement les fondateurs"),
            user_id=user,
            dry_run=True,
        )
    assert {ch["field"] for ch in preview["changes"]} == {"target", "titles"}
    assert preview["applied"] is False and preview["requires_pause"] is False
    c, _ = await _state(cid)
    assert c.target_qualified_count == 100

    # stale preview is refused
    with pytest.raises(Conflict) as stale:
        async with session_scope() as s:
            await csvc.amend_campaign(
                s, ws, cid, CampaignAmendment(add_target=1), user_id=user, base_hash="0" * 32
            )
    assert stale.value.code == "stale_preview"

    async with session_scope() as s:
        out = await csvc.amend_campaign(
            s,
            ws,
            cid,
            CampaignAmendment(instruction="+50 leads et seulement les fondateurs"),
            user_id=user,
            base_hash=preview["base_hash"],
        )
    assert out["applied"] and out["resumed"] and out["status"] == "running"
    c, _ = await _state(cid)
    assert c.target_qualified_count == 150
    assert c.definition["people_filters"]["titles"] == ["Founder", "Co-Founder"]
    assert c.definition_hash != preview["base_hash"]
    async with session_scope() as s:
        title_filter = await s.scalar(
            sa.select(CampaignFilter.value).where(
                CampaignFilter.campaign_id == cid, CampaignFilter.field == "title"
            )
        )
        audit = await s.scalar(
            sa.select(AuditLog).where(AuditLog.campaign_id == cid, AuditLog.action == "campaign.amend")
        )
        cursor_after = {
            x.source_key: dict(x.cursor)
            for x in (
                await s.scalars(sa.select(CampaignSource).where(CampaignSource.campaign_id == cid))
            ).all()
        }
    assert title_filter == ["Founder", "Co-Founder"], "normalized criteria follow the definition"
    assert audit is not None and audit.payload["before"]["target_qualified_count"] == 100
    assert audit.payload["after"]["target_qualified_count"] == 150
    assert cursor_after == cursor_before, "discovery continues where it stopped"
    amended = await _events(cid, "campaign.amended")
    assert amended and {ch["field"] for ch in amended[-1]["changes"]} == {"target", "titles"}

    await drive([cid])
    c, st = await _state(cid)
    assert c.status == CampaignStatus.exhausted
    assert await _delivered(cid) == ["Antoine Lefort", "Claire Fontaine"]
    assert st.qualified == 2 and await _list_size(cid) == 2, "every lead delivered exactly once"
    assert st.companies_evaluated == 5, "work done before the pause is not counted twice"
    # live events: per-candidate stages and outcomes, deliveries carry their candidate id
    stages = await _events(cid, "candidate.stage")
    assert {e["stage"] for e in stages} >= {"website", "crawl"} and all(e["event_id"] for e in stages)
    assert await _events(cid, "candidate.done")
    assert all(e.get("event_id") for e in await _events(cid, "lead.qualified"))


async def test_stopped_campaign_reopens_only_when_the_condition_is_lifted(env, workspace):
    ws, user = workspace
    cid = await _start(ws, user)
    await drive([cid])
    c, st = await _state(cid)
    assert c.status == CampaignStatus.exhausted and st.qualified == 2
    before = await _list_size(cid)

    async with session_scope() as s:
        with pytest.raises(Conflict) as exc:
            await csvc.resume_campaign(s, ws, cid)
    assert exc.value.code == "exhausted" and "Broaden" in (exc.value.hint or "")

    # raising the target alone does not create new work → applied, but resume is blocked with the reason
    async with session_scope() as s:
        out = await csvc.amend_campaign(s, ws, cid, CampaignAmendment(add_target=20), user_id=user)
    assert out["applied"] and not out["resumed"] and out["resume_blocked"]["code"] == "exhausted"
    c, _ = await _state(cid)
    assert c.status == CampaignStatus.exhausted and c.target_qualified_count == 120

    # broadening (another industry) adds discovery queries → the source comes back and the campaign reopens
    async with session_scope() as s:
        out = await csvc.amend_campaign(
            s, ws, cid, CampaignAmendment(instruction="ajoute aussi les agences web"), user_id=user
        )
    assert out["resumed"] and out["new_queries"] >= 1 and out["status"] == "running"
    async with session_scope() as s:
        src = await s.scalar(sa.select(CampaignSource).where(CampaignSource.campaign_id == cid))
        assert (
            src is not None
            and len(src.query_plan) == 2
            and src.query_plan[1]["key"].startswith("fixture:all#")
        )
    await drive([cid])
    c, st = await _state(cid)
    assert c.status == CampaignStatus.exhausted
    assert st.qualified == 2 and await _list_size(cid) == before, "already delivered leads are never repeated"


async def test_target_and_budget_stops_reopen_after_raising_them(workspace):
    ws, user = workspace
    defn, _ = await parse_prompt("Find 10 marketing agencies in Lyon, founders")
    async with session_scope() as s:
        c = await csvc.create_campaign(s, ws, defn, user_id=user, prompt="x")
        cid = c.id
        # one candidate is mid-pipeline when the stop happens
        comp = Company(workspace_id=ws, name="Studio", normalized_name="studio")
        s.add(comp)
        await s.flush()
        s.add(
            CompanyDiscoveryEvent(
                workspace_id=ws,
                campaign_id=cid,
                company_id=comp.id,
                source_key="fixture",
                candidate_key="k1",
                name="Studio",
                stage="crawl",
            )
        )
    async with session_scope() as s:
        c = await s.get(Campaign, cid)
        await csvc.stop_campaign(s, c, CampaignStatus.budget_reached, "Campaign budget reached")
        c.max_cost_usd = Decimal("1.00")
        st = await s.get(CampaignStats, cid)
        st.cost_usd = Decimal("1.20")
    async with session_scope() as s:
        with pytest.raises(Conflict) as exc:
            await csvc.resume_campaign(s, ws, cid)
    assert exc.value.code == "budget_reached"
    async with session_scope() as s:
        out = await csvc.amend_campaign(s, ws, cid, CampaignAmendment(instruction="budget 5 $"), user_id=user)
    assert out["resumed"] and out["status"] == "running"
    assert out["changes"] == [
        {"field": "budget", "label": "Budget", "before": "No limit", "after": "$5.00", "safe": True}
    ]
    c, _ = await _state(cid)
    assert c.stop_reason is None and float(c.max_cost_usd or 0) == 5.0

    # completed (target reached) → "+N leads" reopens it
    async with session_scope() as s:
        c = await s.get(Campaign, cid)
        st = await s.get(CampaignStats, cid)
        st.qualified = 10
        await csvc.stop_campaign(s, c, CampaignStatus.completed, "Target reached")
    async with session_scope() as s:
        with pytest.raises(Conflict) as exc:
            await csvc.resume_campaign(s, ws, cid)
    assert exc.value.code == "target_reached"
    async with session_scope() as s:
        out = await csvc.amend_campaign(s, ws, cid, CampaignAmendment(instruction="+5 leads"), user_id=user)
    assert out["resumed"]
    async with session_scope() as s:
        jobs = (
            await s.scalars(sa.select(Job).where(Job.campaign_id == cid, Job.type == "company.process"))
        ).all()
    active = [j.status for j in jobs if j.status != JobStatus.cancelled]
    assert active == [JobStatus.pending], "the candidate that was mid-pipeline gets its job back (once)"


async def test_running_campaign_safe_vs_criteria_changes_and_lifecycle_rules(workspace):
    ws, user = workspace
    defn, _ = await parse_prompt("Find 10 marketing agencies in Lyon, founders")
    async with session_scope() as s:
        c = await csvc.create_campaign(s, ws, defn, user_id=user, prompt="x")
        cid = c.id
        c.status = CampaignStatus.running
    async with session_scope() as s:
        preview = await csvc.amend_campaign(
            s, ws, cid, CampaignAmendment(add_target=5), user_id=user, dry_run=True
        )
    assert preview["requires_pause"] is False
    async with session_scope() as s:
        out = await csvc.amend_campaign(s, ws, cid, CampaignAmendment(add_target=5), user_id=user)
    assert out["auto_paused"] is False and out["status"] == "running"
    async with session_scope() as s:
        preview = await csvc.amend_campaign(
            s, ws, cid, CampaignAmendment(instruction="ajoute Marseille"), user_id=user, dry_run=True
        )
    assert preview["requires_pause"] is True
    async with session_scope() as s:
        out = await csvc.amend_campaign(
            s, ws, cid, CampaignAmendment(instruction="ajoute Marseille"), user_id=user
        )
    assert out["auto_paused"] and out["resumed"] and out["status"] == "running"
    statuses = [e["status"] for e in await _events(cid, "campaign.status")]
    assert statuses[-1] == "running", "an auto-pause for a criteria change ends running again"

    # idempotent lifecycle
    async with session_scope() as s:
        assert (await csvc.resume_campaign(s, ws, cid)).status == CampaignStatus.running
    async with session_scope() as s:
        await csvc.cancel_campaign(s, ws, cid)
    async with session_scope() as s:
        with pytest.raises(Conflict) as exc:
            await csvc.resume_campaign(s, ws, cid)
    assert exc.value.code == "cancelled"
    async with session_scope() as s:
        with pytest.raises(Conflict):
            await csvc.amend_campaign(s, ws, cid, CampaignAmendment(add_target=1), user_id=user)
        with pytest.raises(Conflict) as exc:
            await csvc.pause_campaign(s, ws, cid)
    assert exc.value.code == "not_running"


async def test_pause_during_planning_is_not_overridden(env, workspace):
    from scout.jobs.worker import Worker
    from scout.main import load_handlers

    ws, user = workspace
    cid = await _start(ws, user)
    async with session_scope() as s:
        await csvc.pause_campaign(s, ws, cid)
    load_handlers()
    await Worker(slots=2).run_until_idle(timeout_s=20, idle_rounds=2)
    c, st = await _state(cid)
    assert c.status == CampaignStatus.paused and st.raw_discovered == 0
    async with session_scope() as s:
        plan = await s.scalar(sa.select(Job).where(Job.campaign_id == cid, Job.type == "campaign.plan"))
    assert plan is not None and plan.status == JobStatus.paused
    async with session_scope() as s:
        await csvc.resume_campaign(s, ws, cid)
    await drive([cid])
    c, st = await _state(cid)
    assert c.status == CampaignStatus.exhausted and st.qualified == 2


async def test_live_snapshot_kick_and_http_routes(env, workspace):
    from scout.auth.context import issue_service_token
    from scout.main import create_app

    ws, user = workspace
    cid = await _start(ws, user)
    async with session_scope() as s:
        await s.execute(
            sa.update(Job)
            .where(Job.campaign_id == cid)
            .values(run_after=sa.func.now() + sa.text("interval '1 hour'"))
        )
        c = await s.get(Campaign, cid)
        c.status = CampaignStatus.running
    app = create_app()
    headers = {
        "Authorization": f"Bearer {issue_service_token(user, email=f'{user}@example.com')}",
        "X-Workspace-Id": str(ws),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://scout.test"
    ) as client:
        r = await client.get(f"/v1/campaigns/{cid}/live", headers=headers)
        assert r.status_code == 200, r.text
        live = r.json()
        assert {"in_flight", "stages", "recent", "health"} <= set(live)
        assert {"last_event_at", "overdue_jobs", "jobs", "workers_enabled"} <= set(live["health"])
        r = await client.post(f"/v1/campaigns/{cid}/kick", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["kick"]["woken"] >= 1
        r = await client.patch(
            f"/v1/campaigns/{cid}", headers=headers, json={"instruction": "+10 leads", "dry_run": True}
        )
        assert r.status_code == 200, r.text
        assert r.json()["changes"][0]["after"] == "110"
        r = await client.post(f"/v1/campaigns/{cid}/amend", headers=headers, json={"add_target": 10})
        assert r.status_code == 200, r.text
        assert r.json()["campaign"]["target"] == 110
        r = await client.post(f"/v1/campaigns/{cid}/amend", headers=headers, json={"instruction": "blabla"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "amend_not_understood"
        r = await client.post("/v1/campaigns/clarify", headers=headers, json={"prompt": "Find dentists"})
        assert r.status_code == 200 and len(r.json()["questions"]) == 3
        r = await client.post(
            "/v1/campaigns/plan",
            headers=headers,
            json={
                "prompt": "Find marketing agencies",
                "answers": [{"id": "geography", "value": "Lyon"}, {"id": "volume", "value": "30"}],
            },
        )
        assert r.status_code == 200, r.text
        planned = r.json()
        assert planned["definition"]["company_filters"]["cities"] == ["Lyon"]
        assert planned["definition"]["target_qualified_count"] == 30
        assert {"Location", "Target"} <= {i["label"] for i in planned["interpretation"]}
    async with session_scope() as s:
        delayed = await s.scalar(
            sa.select(sa.func.count())
            .select_from(Job)
            .where(Job.campaign_id == cid, Job.run_after > sa.func.now())
        )
    assert delayed == 0, "Retry wakes every delayed job of the campaign"


async def test_first_page_load_burst_resolves_to_a_single_workspace(db):
    """Several requests fire at once on the first page load (rows, lists, the live event stream…). They must all
    land in the same workspace — otherwise the live stream can bind to an empty one and never show progress."""
    import asyncio

    from scout.auth.context import Principal, ensure_user_and_workspace
    from scout.db.models import User, WorkspaceMember

    async with session_scope() as s:
        s.add(User(id="user_burst", name="Burst", email="burst@example.com"))
    p = Principal(user_id="user_burst", email="burst@example.com", name="Burst")
    results = await asyncio.gather(*[ensure_user_and_workspace(p) for _ in range(6)])
    assert len({r[0][0].id for r in results}) == 1
    async with session_scope() as s:
        n = await s.scalar(
            sa.select(sa.func.count())
            .select_from(WorkspaceMember)
            .where(WorkspaceMember.user_id == "user_burst")
        )
    assert n == 1


async def test_amend_is_undoable_and_restores_criteria_target_and_filters(workspace):
    from scout.errors import ValidationFailed
    from scout.services.undo import undo

    ws, user = workspace
    defn, _ = await parse_prompt("Find 10 marketing agencies in Lyon, founders")
    async with session_scope() as s:
        c = await csvc.create_campaign(s, ws, defn, user_id=user, prompt="x")
        cid = c.id
        c.status = CampaignStatus.running
    original, _ = await _state(cid)
    original_def, original_hash = original.definition, original.definition_hash

    async def filters() -> list[tuple[str, str]]:
        async with session_scope() as s:
            rows = (await s.scalars(sa.select(CampaignFilter).where(CampaignFilter.campaign_id == cid))).all()
            return sorted((r.field, str(r.value)) for r in rows)

    original_filters = await filters()
    async with session_scope() as s:
        out = await csvc.amend_campaign(
            s, ws, cid, CampaignAmendment(instruction="ajoute Marseille, +5 leads"), user_id=user
        )
    assert out["applied"] and out["audit_id"]
    changed, _ = await _state(cid)
    assert changed.definition_hash != original_hash and changed.target_qualified_count == 15
    assert await filters() != original_filters

    async with session_scope() as s:
        res = await undo(s, ws, out["audit_id"], user_id=user)
    assert res["base_hash"] == original_hash
    restored, _ = await _state(cid)
    assert restored.definition == original_def and restored.definition_hash == original_hash
    assert restored.target_qualified_count == 10
    assert restored.status == CampaignStatus.running, (
        "undo mid-run pauses → applies → resumes, like the amend"
    )
    assert await filters() == original_filters
    undone = [e for e in await _events(cid, "campaign.amended") if e.get("undone")]
    assert undone and {ch["field"] for ch in undone[-1]["changes"]} >= {"location", "target"}
    async with session_scope() as s:
        with pytest.raises(Conflict):
            await undo(s, ws, out["audit_id"], user_id=user)  # already undone

    # an older change can't be undone over a newer one
    async with session_scope() as s:
        first = await csvc.amend_campaign(s, ws, cid, CampaignAmendment(add_target=1), user_id=user)
    async with session_scope() as s:
        await csvc.amend_campaign(s, ws, cid, CampaignAmendment(add_target=2), user_id=user)
    async with session_scope() as s:
        with pytest.raises(Conflict) as exc:
            await undo(s, ws, first["audit_id"], user_id=user)
    assert exc.value.code == "stale_undo"

    # nor on a cancelled search
    async with session_scope() as s:
        last = await csvc.amend_campaign(s, ws, cid, CampaignAmendment(add_target=3), user_id=user)
    async with session_scope() as s:
        await csvc.cancel_campaign(s, ws, cid)
    async with session_scope() as s:
        with pytest.raises((Conflict, ValidationFailed)):
            await undo(s, ws, last["audit_id"], user_id=user)
