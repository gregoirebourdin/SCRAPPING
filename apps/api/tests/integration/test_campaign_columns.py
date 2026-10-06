"""Columns a search asks for ("ManyChat" as a bonus) exist on its list once, and each delivered lead gets them."""

from __future__ import annotations

import types
import uuid

import sqlalchemy as sa

from scout.ai.factory import LocalProvider, set_ai
from scout.db.engine import session_scope
from scout.db.enums import EntityType
from scout.db.models import CustomColumn, Job
from scout.pipeline.campaigns import ensure_campaign_columns
from scout.pipeline.processor import _run_campaign_columns
from scout.schemas.campaign import CampaignDefinition, EnrichmentRequest
from scout.services import lists as lists_svc

pytestmark = __import__("pytest").mark.integration


async def test_requested_columns_are_created_once_and_run_per_lead(workspace):
    ws, user = workspace
    set_ai(LocalProvider())
    try:
        async with session_scope() as s:
            lst, _ = await lists_svc.create_list(
                s, ws, name="ManyChat agencies", user_id=user, entity_type=EntityType.person
            )
        defn = CampaignDefinition(
            enrichments=[EnrichmentRequest(name="ManyChat", instruction="Uses ManyChat?")]
        )
        raw = defn.model_dump(mode="json")
        assert await ensure_campaign_columns(ws, lst.id, raw) == 1
        assert await ensure_campaign_columns(ws, lst.id, raw) == 0  # re-planned or resumed: never a duplicate
        async with session_scope() as s:
            cols = (await s.scalars(sa.select(CustomColumn).where(CustomColumn.list_id == lst.id))).all()
        assert [c.name for c in cols] == ["ManyChat"] and cols[0].entity_type == EntityType.company

        company_id = uuid.uuid4()
        ctx = types.SimpleNamespace(
            defn=defn, target_list_id=lst.id, workspace_id=ws, campaign_id=uuid.uuid4()
        )
        await _run_campaign_columns(ctx, company_id=company_id, person_id=uuid.uuid4())  # type: ignore[arg-type]
        async with session_scope() as s:
            jobs = (
                await s.scalars(sa.select(Job).where(Job.workspace_id == ws, Job.type == "enrichment.batch"))
            ).all()
        assert len(jobs) == 1 and jobs[0].campaign_id is None
        assert jobs[0].payload["column_id"] == str(cols[0].id)
        assert jobs[0].payload["entity_ids"] == [str(company_id)]
    finally:
        set_ai(None)
