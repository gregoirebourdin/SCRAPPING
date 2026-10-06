"""Dynamic custom columns: plan, create (+enrich), edit definition, enrich/refresh, user cell edits."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter

from scout.api.deps import Ctx
from scout.db.engine import session_scope
from scout.db.enums import ColumnDataType, EntityType
from scout.db.models import CustomColumn, CustomFieldValue
from scout.errors import NotFound
from scout.schemas.api import CellEdit, ColumnCreate, ColumnOut, ColumnUpdate, EnrichRequest
from scout.services import audit
from scout.services import lists as lists_svc

router = APIRouter(tags=["columns"])


async def _get_column(s: Any, ctx: Ctx, column_id: uuid.UUID) -> CustomColumn:
    col = await s.get(CustomColumn, column_id)
    if col is None or col.workspace_id != ctx.workspace_id:
        raise NotFound("Column not found")
    return col


@router.get("/columns", response_model=list[ColumnOut])
async def list_columns(ctx: Ctx, list_id: uuid.UUID | None = None) -> list[CustomColumn]:
    from scout.query.rows import load_columns

    async with session_scope() as s:
        return await load_columns(s, ctx.workspace_id, list_id)


@router.post("/columns/plan")
async def plan(body: ColumnCreate, ctx: Ctx) -> dict[str, Any]:
    from scout.enrich.planner import describe_plan, plan_column

    p = await plan_column(
        body.name, body.instruction, data_type=ColumnDataType(body.data_type) if body.data_type else None
    )
    return {"plan": p.model_dump(mode="json"), "describe": describe_plan(p)}


@router.post("/columns", status_code=201)
async def create_column(body: ColumnCreate, ctx: Ctx) -> dict[str, Any]:
    from scout.enrich.engine import (
        column_entity_ids,
        create_column,
        enqueue_column,
        estimate_coverage,
    )
    from scout.enrich.planner import describe_plan
    from scout.enrich.types import EnrichmentPlan

    col = await create_column(
        ctx.workspace_id,
        name=body.name,
        instruction=body.instruction or body.name,
        list_id=body.list_id,
        data_type=ColumnDataType(body.data_type) if body.data_type else None,
        created_by=ctx.user_id,
    )
    plan_obj = EnrichmentPlan.model_validate(col.configuration)
    coverage: dict[str, Any] = {}
    queued = 0
    async with session_scope() as s:
        entity_ids = None
        if body.rows is not None:
            rows = await lists_svc.resolve_rows(s, ctx.workspace_id, body.rows)
            entity_ids = rows.ids
        await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="column.create",
            entity_type="column",
            entity_ids=[col.id],
            summary=f'Created column "{col.name}"',
            undo={"op": "delete_column", "column_id": str(col.id)},
        )
    async with session_scope() as s:
        col_db = await _get_column(s, ctx, col.id)
        ids = await column_entity_ids(
            ctx.workspace_id,
            col_db,
            list_id=body.list_id,
            person_ids=entity_ids if body.rows and body.rows.entity_type == EntityType.person else None,
            company_ids=entity_ids if body.rows and body.rows.entity_type == EntityType.company else None,
        )
        coverage = await estimate_coverage(ctx.workspace_id, col_db, ids)
    if body.run:
        queued = await enqueue_column(ctx.workspace_id, col.id, entity_ids=ids, list_id=body.list_id)
    return {
        "column": ColumnOut.model_validate(col).model_dump(mode="json"),
        "plan": plan_obj.model_dump(mode="json"),
        "describe": describe_plan(plan_obj),
        "coverage": coverage,
        "queued": queued,
    }


@router.patch("/columns/{column_id}", response_model=ColumnOut)
async def update_column(column_id: uuid.UUID, body: ColumnUpdate, ctx: Ctx) -> CustomColumn:
    from scout.enrich.engine import update_column_definition

    async with session_scope() as s:
        col = await _get_column(s, ctx, column_id)
        old_name = col.name
        if body.name:
            col.name = body.name.strip()
        if body.is_hidden is not None:
            col.is_hidden = body.is_hidden
        if body.position is not None:
            col.position = body.position
        await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="column.update",
            entity_type="column",
            entity_ids=[column_id],
            summary=f'Updated column "{col.name}"',
            payload=body.model_dump(exclude_none=True),
        )
    if any(
        v is not None
        for v in (
            body.instruction,
            body.data_type,
            body.confidence_threshold,
            body.refresh_days,
            body.source_preferences,
        )
    ):
        await update_column_definition(
            ctx.workspace_id,
            column_id,
            instruction=body.instruction,
            data_type=ColumnDataType(body.data_type) if body.data_type else None,
            confidence_threshold=body.confidence_threshold,
            refresh_days=body.refresh_days,
            source_preferences=body.source_preferences,
        )
    async with session_scope() as s:
        col = await _get_column(s, ctx, column_id)
        _ = old_name
        return col


@router.delete("/columns/{column_id}")
async def delete_column(column_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        col = await _get_column(s, ctx, column_id)
        name = col.name
        await s.delete(col)
        await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="column.delete",
            entity_type="column",
            entity_ids=[column_id],
            summary=f'Deleted column "{name}"',
        )
    return {"deleted": True}


@router.post("/columns/{column_id}/duplicate", response_model=ColumnOut)
async def duplicate_column(column_id: uuid.UUID, ctx: Ctx) -> CustomColumn:
    async with session_scope() as s:
        col = await _get_column(s, ctx, column_id)
        n = 2
        while await s.scalar(
            sa.select(CustomColumn.id).where(
                CustomColumn.workspace_id == ctx.workspace_id,
                CustomColumn.list_id == col.list_id if col.list_id else CustomColumn.list_id.is_(None),
                CustomColumn.slug == f"{col.slug}_{n}",
            )
        ):
            n += 1
        dup = CustomColumn(
            workspace_id=ctx.workspace_id,
            list_id=col.list_id,
            name=f"{col.name} ({n})",
            slug=f"{col.slug}_{n}",
            data_type=col.data_type,
            kind=col.kind,
            entity_type=col.entity_type,
            resolver_type=col.resolver_type,
            instructions=col.instructions,
            configuration=col.configuration,
            source_preferences=col.source_preferences,
            confidence_threshold=col.confidence_threshold,
            refresh_policy=col.refresh_policy,
            position=col.position + 1,
            created_by=ctx.user_id,
        )
        s.add(dup)
        await s.flush()
        return dup


@router.post("/columns/{column_id}/enrich")
async def enrich(column_id: uuid.UUID, body: EnrichRequest, ctx: Ctx) -> dict[str, Any]:
    from scout.enrich.engine import column_entity_ids, enqueue_column

    async with session_scope() as s:
        col = await _get_column(s, ctx, column_id)
        person_ids = company_ids = None
        if body.rows is not None:
            rows = await lists_svc.resolve_rows(s, ctx.workspace_id, body.rows)
            if rows.entity_type == EntityType.person:
                person_ids = rows.ids
            else:
                company_ids = rows.ids
        ids = await column_entity_ids(
            ctx.workspace_id,
            col,
            list_id=body.list_id or col.list_id,
            person_ids=person_ids,
            company_ids=company_ids,
        )
    queued = await enqueue_column(
        ctx.workspace_id, column_id, entity_ids=ids, only_missing=body.only_missing, force=body.force
    )
    return {"queued": queued}


@router.put("/columns/{column_id}/cells")
async def edit_cell(column_id: uuid.UUID, body: CellEdit, ctx: Ctx) -> dict[str, Any]:
    from scout.enrich.engine import set_user_value

    async with session_scope() as s:
        await _get_column(s, ctx, column_id)
        prev = await s.scalar(
            sa.select(CustomFieldValue).where(
                CustomFieldValue.column_id == column_id, CustomFieldValue.entity_id == body.entity_id
            )
        )
        previous = (
            None
            if prev is None
            else {
                "value": prev.value_json,
                "display": prev.display_value,
                "status": prev.status.value,
                "user": prev.is_user_override,
            }
        )
    v = await set_user_value(
        ctx.workspace_id, column_id, body.entity_type, body.entity_id, body.value, user_id=ctx.user_id
    )
    async with session_scope() as s:
        entry = await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="cell.edit",
            entity_type=body.entity_type.value,
            entity_ids=[body.entity_id],
            summary="Edited a cell",
            undo={
                "op": "restore_cells",
                "column_id": str(column_id),
                "cells": [{"entity_id": str(body.entity_id), "previous": previous}],
            },
        )
    return {"ok": True, "display_value": v.display_value, "audit_id": entry.id}
