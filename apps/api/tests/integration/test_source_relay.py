"""A Maps scraper that keeps failing mid-campaign hands over to the official Places API."""

from __future__ import annotations

import sqlalchemy as sa

from scout.config import get_settings
from scout.db.engine import session_scope
from scout.db.models import CampaignSource, Job
from scout.pipeline import campaigns as csvc
from scout.pipeline.icp import parse_prompt
from scout.pipeline.jobs import RELAY_AFTER_ERRORS, _maybe_relay

pytestmark = __import__("pytest").mark.integration


async def test_failing_scraper_hands_over_to_places_api(workspace, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    get_settings.cache_clear()
    try:
        ws, user = workspace
        defn, _ = await parse_prompt("Find 10 dentists in Lyon")
        async with session_scope() as s:
            c = await csvc.create_campaign(s, ws, defn, user_id=user, prompt="x")
            cid = c.id
            s.add(
                CampaignSource(
                    campaign_id=cid, source_key="google_maps", priority=50, query_plan=[], cursor={}
                )
            )
        assert not await _maybe_relay(ws, cid, "google_maps", defn)  # healthy so far: no relay
        async with session_scope() as s:
            await s.execute(
                sa.update(CampaignSource)
                .where(CampaignSource.campaign_id == cid, CampaignSource.source_key == "google_maps")
                .values(error_count=RELAY_AFTER_ERRORS)
            )
        assert await _maybe_relay(ws, cid, "google_maps", defn)
        assert not await _maybe_relay(ws, cid, "google_maps", defn)  # idempotent
        async with session_scope() as s:
            relay = await s.scalar(
                sa.select(CampaignSource).where(
                    CampaignSource.campaign_id == cid, CampaignSource.source_key == "google_places"
                )
            )
            jobs = (
                await s.scalars(sa.select(Job).where(Job.campaign_id == cid, Job.type == "campaign.discover"))
            ).all()
        assert relay is not None and relay.query_plan and relay.priority == 50
        assert [j.payload["source_key"] for j in jobs] == ["google_places"]
        defn.sources.excluded = ["google_places"]
        assert not await _maybe_relay(ws, cid, "google_maps", defn)  # never against the user's exclusion
    finally:
        get_settings.cache_clear()
