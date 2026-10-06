"""Per-lead enrichment values with their evidence, for the lead drawer and source inspector (spec §83–§84)."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import EntityType
from scout.db.models import CustomColumn, CustomFieldValue
from scout.services.freshness import freshness_label


async def lead_enrichments(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    person_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    targets: list[sa.ColumnElement[bool]] = []
    if person_id:
        targets.append(
            sa.and_(
                CustomFieldValue.entity_type == EntityType.person, CustomFieldValue.entity_id == person_id
            )
        )
    if company_id:
        targets.append(
            sa.and_(
                CustomFieldValue.entity_type == EntityType.company, CustomFieldValue.entity_id == company_id
            )
        )
    if not targets:
        return []
    rows = (
        await s.execute(
            sa.select(
                CustomFieldValue,
                CustomColumn.name,
                CustomColumn.data_type,
                CustomColumn.kind,
                CustomColumn.list_id,
            )
            .join(CustomColumn, CustomColumn.id == CustomFieldValue.column_id)
            .where(CustomFieldValue.workspace_id == workspace_id, sa.or_(*targets))
            .order_by(CustomColumn.position, CustomColumn.name)
        )
    ).all()
    out: list[dict[str, Any]] = []
    for v, name, data_type, kind, list_id in rows:
        out.append(
            {
                "column_id": v.column_id,
                "column": name,
                "data_type": data_type.value,
                "kind": kind.value,
                "list_id": list_id,
                "entity_type": v.entity_type.value,
                "value": v.value_json,
                "display": v.display_value,
                "status": v.status.value,
                "confidence": v.confidence,
                "evidence": v.evidence,
                "source_url": v.source_url,
                "resolver": v.resolver,
                "user_override": v.is_user_override,
                "error": v.error,
                "observed_at": v.observed_at or v.updated_at,
                "freshness": freshness_label(v.observed_at or v.updated_at),
            }
        )
    return out
