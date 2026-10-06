"""Import (CSV + mapping), export (CSV/JSON, injection-safe), suppression list."""

from __future__ import annotations

import uuid
from typing import Any

import orjson
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import Response

from scout.api.deps import Ctx
from scout.db.engine import session_scope
from scout.db.enums import EntityType, MemberRole
from scout.errors import ValidationFailed
from scout.schemas.api import ExportRequest, SuppressRequest
from scout.services import audit, exports, imports, suppression
from scout.services import lists as lists_svc

router = APIRouter(tags=["io"])

MAX_UPLOAD = 20 * 1024 * 1024


async def _read(file: UploadFile) -> bytes:
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise ValidationFailed("File too large (max 20 MB)")
    return raw


@router.post("/imports/preview")
async def import_preview(ctx: Ctx, file: UploadFile = File(...)) -> dict[str, Any]:
    return imports.preview(await _read(file))


@router.post("/imports", status_code=202)
async def import_start(
    ctx: Ctx,
    file: UploadFile = File(...),
    mapping: str = Form(...),
    list_id: str | None = Form(default=None),
    mark_as_known: bool = Form(default=True),
) -> dict[str, Any]:
    try:
        mapping_obj = orjson.loads(mapping)
    except orjson.JSONDecodeError as exc:
        raise ValidationFailed("Invalid mapping JSON") from exc
    raw = await _read(file)
    async with session_scope() as s:
        lid = uuid.UUID(list_id) if list_id else None
        if lid:
            await lists_svc.get_list(s, ctx.workspace_id, lid)
        imp = await imports.start_import(s, ctx.workspace_id, raw=raw, filename=file.filename or "import.csv",
                                         mapping=mapping_obj, list_id=lid, mark_as_known=mark_as_known, user_id=ctx.user_id)
        await audit.log(s, workspace_id=ctx.workspace_id, actor_id=ctx.user_id, action="import.start",
                        summary=f"Import of {imp.row_count} rows from {imp.filename}", payload={"import_id": str(imp.id)})
        return {"id": imp.id, "row_count": imp.row_count, "status": imp.status.value}


@router.get("/imports/{import_id}")
async def import_status(import_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        return await imports.get_import(s, ctx.workspace_id, import_id)


@router.post("/exports")
async def export(body: ExportRequest, ctx: Ctx) -> Response:
    entity = body.entity_type
    async with session_scope() as s:
        name = "scout-export"
        if body.list_id:
            lst = await lists_svc.get_list(s, ctx.workspace_id, body.list_id)
            entity = lst.entity_type
            name = "".join(ch if ch.isalnum() else "-" for ch in lst.name.lower()).strip("-")[:40] or name
        res = await exports.export_rows(
            s, ctx.workspace_id, user_id=ctx.user_id, entity_type=entity, scope=body.scope, list_id=body.list_id,
            view_id=body.view_id, ids=body.ids, filters=body.filters, sort=body.sort, search=body.search,
            columns=body.columns, columns_mode=body.columns_mode, fmt=body.format, filename_hint=name,
        )
        await audit.log(s, workspace_id=ctx.workspace_id, actor_id=ctx.user_id, action="export",
                        entity_type=entity.value, summary=f"Exported {res.row_count} rows ({body.format.upper()})",
                        payload={"export_id": str(res.export_id), "scope": body.scope.value})
    return Response(
        content=res.body, media_type=res.content_type,
        headers={"Content-Disposition": f'attachment; filename="{res.filename}"', "X-Row-Count": str(res.row_count),
                 "X-Export-Id": str(res.export_id)},
    )


@router.get("/suppression")
async def list_suppression(ctx: Ctx) -> list[dict[str, Any]]:
    async with session_scope() as s:
        return await suppression.list_suppressions(s, ctx.workspace_id)


@router.post("/suppression")
async def suppress(body: SuppressRequest, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        person_ids: list[uuid.UUID] = []
        company_ids: list[uuid.UUID] = []
        if body.rows is not None:
            rows = await lists_svc.resolve_rows(s, ctx.workspace_id, body.rows)
            (person_ids if rows.entity_type == EntityType.person else company_ids).extend(rows.ids)
        n = await suppression.suppress(s, ctx.workspace_id, reason=body.reason, user_id=ctx.user_id, person_ids=person_ids,
                                       company_ids=company_ids, emails=body.emails, domains=body.domains, note=body.note)
        await audit.log(s, workspace_id=ctx.workspace_id, actor_id=ctx.user_id, action="suppress",
                        entity_ids=[*person_ids, *company_ids], summary=f"Suppressed {n} entries ({body.reason.value})")
    return {"suppressed": n}


@router.delete("/suppression/{entry_id}")
async def unsuppress(entry_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    ctx.require(MemberRole.admin)
    async with session_scope() as s:
        await suppression.unsuppress(s, ctx.workspace_id, entry_id)
        await audit.log(s, workspace_id=ctx.workspace_id, actor_id=ctx.user_id, action="unsuppress", summary="Removed a suppression entry")
    return {"ok": True}
