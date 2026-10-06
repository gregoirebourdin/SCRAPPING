"""Load a campaign's compiled exclusion rules from campaign_exclusions."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import ExposureType
from scout.db.models import CampaignExclusion
from scout.services.exclusion import ExclusionRule


async def load_rules(s: AsyncSession, campaign_id: uuid.UUID) -> list[ExclusionRule]:
    rows = (await s.scalars(sa.select(CampaignExclusion).where(CampaignExclusion.campaign_id == campaign_id))).all()
    return [
        ExclusionRule(
            entity=r.entity,
            exposure_types=[ExposureType(t) for t in r.exposure_types] if r.exposure_types else None,
            within_days=r.within_days,
            list_ids=list(r.list_ids or []),
            campaign_ids=list(r.campaign_ids or []),
            import_ids=list(r.import_ids or []),
            include_list_history=r.include_list_history,
            mode=r.mode,
        )
        for r in rows
    ]
