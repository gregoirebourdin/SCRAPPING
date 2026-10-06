"""Lists, memberships, rows (table data) and saved views."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from scout.api.deps import Ctx
from scout.db.engine import session_scope
from scout.db.enums import EntityType
from scout.errors import ValidationFailed
from scout.query.rows import RowScope, query_rows
from scout.schemas.api import (
    ColumnOut,
    ListCreate,
    ListOut,
    ListUpdate,
    MembershipRequest,
    MembershipResult,
    MoveRequest,
    RowsOut,
    RowsQuery,
    ViewCreate,
    ViewOut,
    ViewUpdate,
)
from scout.services import audit
from scout.services import lists as lists_svc
from scout.services import views as views_svc

router = APIRouter(tags=["lists"])


@router.get("/lists", response_model=list[ListOut])
async def get_lists(ctx: Ctx, include_archived: bool = False) -> list[dict[str, Any]]:
    async with session_scope() as s:
        return await lists_svc.list_overview(s, ctx.workspace_id, include_archived=include_archived)


@router.post("/lists", response_model=ListOut, status_code=201)
async def create_list(body: ListCreate, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        lst, _ = await lists_svc.create_list(
            s,
            ctx.workspace_id,
            name=body.name,
            user_id=ctx.user_id,
            entity_type=body.entity_type,
            description=body.description,
            color=body.color,
            filter_snapshot=body.from_filter.model_dump(mode="json") if body.from_filter else None,
        )
        count = 0
        if body.from_filter:
            rows = await lists_svc.resolve_rows(s, ctx.workspace_id, body.from_filter)
            change = await lists_svc.add_to_list(
                s,
                ctx.workspace_id,
                lst.id,
                rows.entity_type,
                rows.ids,
                user_id=ctx.user_id,
                added_via="filter",
            )
            count = len(change.added)
        await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="list.create",
            entity_type="list",
            entity_ids=[lst.id],
            summary=f'Created list "{lst.name}"',
            undo={"op": "set_archived", "list_id": str(lst.id), "archived": True},
        )
        return {**_list_dict(lst), "count": count}


def _list_dict(lst: Any) -> dict[str, Any]:
    return {
        "id": lst.id,
        "name": lst.name,
        "description": lst.description,
        "entity_type": lst.entity_type.value,
        "color": lst.color,
        "is_archived": lst.is_archived,
        "created_at": lst.created_at,
        "updated_at": lst.updated_at,
        "source_campaign_id": lst.source_campaign_id,
    }


@router.get("/lists/{list_id}")
async def get_list(list_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        lst = await lists_svc.get_list(s, ctx.workspace_id, list_id)
        summary = await lists_svc.quality_summary(s, ctx.workspace_id, list_id)
        return {**_list_dict(lst), "count": summary.get("total", 0), "summary": summary}


@router.patch("/lists/{list_id}", response_model=ListOut)
async def update_list(list_id: uuid.UUID, body: ListUpdate, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        lst = await lists_svc.get_list(s, ctx.workspace_id, list_id)
        if body.name and body.name.strip() != lst.name:
            _, old = await lists_svc.rename_list(s, ctx.workspace_id, list_id, body.name)
            await audit.log(
                s,
                workspace_id=ctx.workspace_id,
                actor_id=ctx.user_id,
                action="list.rename",
                entity_type="list",
                entity_ids=[list_id],
                summary=f'Renamed list to "{body.name}"',
                undo={"op": "rename_list", "list_id": str(list_id), "name": old},
            )
        if body.description is not None:
            lst.description = body.description
        if body.color is not None:
            lst.color = body.color
        if body.is_archived is not None and body.is_archived != lst.is_archived:
            await lists_svc.set_archived(s, ctx.workspace_id, list_id, body.is_archived)
            await audit.log(
                s,
                workspace_id=ctx.workspace_id,
                actor_id=ctx.user_id,
                action="list.archive" if body.is_archived else "list.unarchive",
                entity_type="list",
                entity_ids=[list_id],
                summary=("Archived" if body.is_archived else "Restored") + f' "{lst.name}"',
                undo={"op": "set_archived", "list_id": str(list_id), "archived": not body.is_archived},
            )
        await s.flush()
        await s.refresh(lst)
        return _list_dict(lst)


@router.delete("/lists/{list_id}")
async def delete_list(list_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        lst = await lists_svc.get_list(s, ctx.workspace_id, list_id)
        name = lst.name
        n = await lists_svc.delete_list(s, ctx.workspace_id, list_id)
        await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="list.delete",
            entity_type="list",
            entity_ids=[list_id],
            summary=f'Deleted list "{name}" ({n} memberships; discovery history kept)',
        )
    return {"deleted": True, "memberships_removed": n}


@router.post("/lists/{list_id}/duplicate", response_model=ListOut)
async def duplicate_list(list_id: uuid.UUID, ctx: Ctx, name: str | None = None) -> dict[str, Any]:
    async with session_scope() as s:
        dst = await lists_svc.duplicate_list(s, ctx.workspace_id, list_id, name=name, user_id=ctx.user_id)
        await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="list.duplicate",
            entity_type="list",
            entity_ids=[dst.id],
            summary=f'Duplicated list as "{dst.name}"',
            undo={"op": "set_archived", "list_id": str(dst.id), "archived": True},
        )
        return _list_dict(dst)


@router.post("/lists/{list_id}/members", response_model=MembershipResult)
async def add_members(list_id: uuid.UUID, body: MembershipRequest, ctx: Ctx) -> MembershipResult:
    async with session_scope() as s:
        rows = await lists_svc.resolve_rows(s, ctx.workspace_id, body.rows)
        change = await lists_svc.add_to_list(
            s, ctx.workspace_id, list_id, rows.entity_type, rows.ids, user_id=ctx.user_id
        )
        entry = await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="list.add",
            entity_type=rows.entity_type.value,
            entity_ids=change.added,
            summary=f"Added {len(change.added)} leads to list",
            undo={
                "op": "remove_from_list",
                "list_id": str(list_id),
                "entity_type": rows.entity_type.value,
                "ids": [str(i) for i in change.added],
            },
        )
        return MembershipResult(
            list_id=list_id,
            affected=len(change.added),
            already_present=change.already_present,
            skipped_suppressed=change.skipped_suppressed,
            audit_id=entry.id,
        )


@router.post("/lists/{list_id}/members/remove", response_model=MembershipResult)
async def remove_members(list_id: uuid.UUID, body: MembershipRequest, ctx: Ctx) -> MembershipResult:
    async with session_scope() as s:
        rows = await lists_svc.resolve_rows(s, ctx.workspace_id, body.rows)
        removed = await lists_svc.remove_from_list(s, ctx.workspace_id, list_id, rows.entity_type, rows.ids)
        entry = await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="list.remove",
            entity_type=rows.entity_type.value,
            entity_ids=removed,
            summary=f"Removed {len(removed)} leads from list (history kept)",
            undo={
                "op": "add_to_list",
                "list_id": str(list_id),
                "entity_type": rows.entity_type.value,
                "ids": [str(i) for i in removed],
            },
        )
        return MembershipResult(list_id=list_id, affected=len(removed), audit_id=entry.id)


@router.post("/lists/{list_id}/members/move", response_model=MembershipResult)
async def move_members(list_id: uuid.UUID, body: MoveRequest, ctx: Ctx) -> MembershipResult:
    async with session_scope() as s:
        rows = await lists_svc.resolve_rows(s, ctx.workspace_id, body.rows)
        change, removed = await lists_svc.move_between_lists(
            s, ctx.workspace_id, list_id, body.to_list_id, rows.entity_type, rows.ids, user_id=ctx.user_id
        )
        entry = await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="list.move",
            entity_type=rows.entity_type.value,
            entity_ids=removed,
            summary=f"Moved {len(removed)} leads",
            undo={
                "op": "move",
                "from_list_id": str(list_id),
                "to_list_id": str(body.to_list_id),
                "entity_type": rows.entity_type.value,
                "ids": [str(i) for i in removed],
                "added_ids": [str(i) for i in change.added],
            },
        )
        return MembershipResult(
            list_id=body.to_list_id,
            affected=len(removed),
            already_present=change.already_present,
            skipped_suppressed=change.skipped_suppressed,
            audit_id=entry.id,
        )


@router.post("/rows/query", response_model=RowsOut, tags=["rows"])
async def rows_query(body: RowsQuery, ctx: Ctx) -> RowsOut:
    if body.scope == "list" and not body.list_id:
        raise ValidationFailed("list_id is required for list scope")
    entity = body.entity_type
    if body.scope == "list" and body.list_id:
        async with session_scope() as s:
            lst = await lists_svc.get_list(s, ctx.workspace_id, body.list_id)
            entity = lst.entity_type
    if body.scope == "companies":
        entity = EntityType.company
    scope = RowScope(kind=body.scope, entity_type=entity, list_id=body.list_id, campaign_id=body.campaign_id)
    async with session_scope() as s:
        res = await query_rows(
            s,
            ctx.workspace_id,
            scope,
            filters=body.filters,
            sort=body.sort,
            search=body.search,
            cursor=body.cursor,
            limit=body.limit,
            with_total=body.with_total,
            ids=body.ids,
        )
    return RowsOut(
        rows=res.rows,
        next_cursor=res.next_cursor,
        total=res.total,
        columns=[ColumnOut.model_validate(c) for c in res.columns],
    )


@router.get("/views", response_model=list[ViewOut], tags=["views"])
async def get_views(
    ctx: Ctx, list_id: uuid.UUID | None = None, entity_type: EntityType = EntityType.person
) -> list[dict[str, Any]]:
    async with session_scope() as s:
        if list_id:
            entity_type = (await lists_svc.get_list(s, ctx.workspace_id, list_id)).entity_type
        return [
            views_svc.view_to_dict(v)
            for v in await views_svc.list_views(s, ctx.workspace_id, list_id=list_id, entity_type=entity_type)
        ]


@router.post("/views", response_model=ViewOut, status_code=201, tags=["views"])
async def create_view(body: ViewCreate, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        v = await views_svc.create_view(
            s,
            ctx.workspace_id,
            name=body.name,
            list_id=body.list_id,
            entity_type=body.entity_type,
            layout=views_svc.ViewLayout(
                filters=body.filters,
                sort=body.sort,
                column_order=body.column_order,
                column_visibility=body.column_visibility,
                column_widths=body.column_widths,
                pinned_columns=body.pinned_columns,
                density=body.density,
            ),
            user_id=ctx.user_id,
        )
        await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="view.create",
            entity_type="view",
            entity_ids=[v.id],
            summary=f'Saved view "{v.name}"',
        )
        return views_svc.view_to_dict(v)


@router.patch("/views/{view_id}", response_model=ViewOut, tags=["views"])
async def update_view(view_id: uuid.UUID, body: ViewUpdate, ctx: Ctx) -> dict[str, Any]:
    layout = body.model_dump(exclude_none=True, exclude={"name"}, mode="json")
    async with session_scope() as s:
        v = await views_svc.update_view(s, ctx.workspace_id, view_id, name=body.name, layout=layout or None)
        await s.flush()
        return views_svc.view_to_dict(v)


@router.delete("/views/{view_id}", tags=["views"])
async def delete_view(view_id: uuid.UUID, ctx: Ctx) -> dict[str, bool]:
    async with session_scope() as s:
        await views_svc.delete_view(s, ctx.workspace_id, view_id)
    return {"deleted": True}
