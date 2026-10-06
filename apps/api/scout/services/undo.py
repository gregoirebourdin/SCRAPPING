"""Undo where realistically safe (spec §114): list moves/removals, renames, archive, manual edits, column creation,
campaign amendments (criteria / target / limits come back; delivered leads stay).

Irreversible external operations (exports downloaded, SMTP probes, crawls) are never offered as undoable.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import EntityType
from scout.db.models import AuditLog, CustomColumn, CustomFieldValue
from scout.errors import Conflict, NotFound, ValidationFailed
from scout.services import audit
from scout.services import lists as lists_svc


async def undo(s: AsyncSession, workspace_id: uuid.UUID, audit_id: int, *, user_id: str) -> dict[str, Any]:
    entry = await s.get(AuditLog, audit_id)
    if entry is None or entry.workspace_id != workspace_id:
        raise NotFound("Action not found")
    if entry.undone_at is not None:
        raise Conflict("This action was already undone")
    payload = entry.undo_payload
    if not payload:
        raise ValidationFailed("This action cannot be undone")
    result = await apply_undo(s, workspace_id, payload, user_id=user_id)
    await audit.mark_undone(s, audit_id)
    await audit.log(
        s,
        workspace_id=workspace_id,
        actor_id=user_id,
        action="undo",
        summary=f"Undid: {entry.summary or entry.action}",
        payload={"audit_id": audit_id},
    )
    return result


async def apply_undo(
    s: AsyncSession, workspace_id: uuid.UUID, p: dict[str, Any], *, user_id: str
) -> dict[str, Any]:
    op = p.get("op")
    et = EntityType(p.get("entity_type", "person"))
    ids = [uuid.UUID(x) for x in p.get("ids", [])]
    if op == "remove_from_list":
        removed = await lists_svc.remove_from_list(s, workspace_id, uuid.UUID(p["list_id"]), et, ids)
        return {"removed": len(removed)}
    if op == "add_to_list":
        change = await lists_svc.add_to_list(
            s, workspace_id, uuid.UUID(p["list_id"]), et, ids, user_id=user_id, added_via="manual"
        )
        return {"added": len(change.added)}
    if op == "move":
        await lists_svc.add_to_list(s, workspace_id, uuid.UUID(p["from_list_id"]), et, ids, user_id=user_id)
        await lists_svc.remove_from_list(
            s, workspace_id, uuid.UUID(p["to_list_id"]), et, [uuid.UUID(x) for x in p.get("added_ids", [])]
        )
        return {"moved_back": len(ids)}
    if op == "rename_list":
        lst, _ = await lists_svc.rename_list(s, workspace_id, uuid.UUID(p["list_id"]), p["name"])
        return {"name": lst.name}
    if op == "set_archived":
        lst = await lists_svc.set_archived(s, workspace_id, uuid.UUID(p["list_id"]), bool(p["archived"]))
        return {"archived": lst.is_archived}
    if op == "delete_column":
        col = await s.get(CustomColumn, uuid.UUID(p["column_id"]))
        if col is None or col.workspace_id != workspace_id:
            raise NotFound("Column not found")
        await s.delete(col)
        return {"deleted_column": p["column_id"]}
    if op == "restore_cells":
        for cell in p.get("cells", []):
            v = await s.scalar(
                sa.select(CustomFieldValue).where(
                    CustomFieldValue.column_id == uuid.UUID(p["column_id"]),
                    CustomFieldValue.entity_id == uuid.UUID(cell["entity_id"]),
                )
            )
            if v is None:
                continue
            if cell.get("previous") is None:
                await s.delete(v)
            else:
                prev = cell["previous"]
                v.value_json, v.display_value, v.status = (
                    prev.get("value"),
                    prev.get("display"),
                    prev.get("status", "success"),
                )
                v.is_user_override = bool(prev.get("user", False))
        return {"restored": len(p.get("cells", []))}
    if op == "restore_field":
        from scout.services.leads import edit_field

        await edit_field(
            s, workspace_id, et, uuid.UUID(p["entity_id"]), p["field"], p["previous"], user_id=user_id
        )
        return {"restored": p["field"]}
    if op == "restore_campaign_definition":
        from scout.pipeline.campaigns import restore_definition

        c = await restore_definition(
            s,
            workspace_id,
            uuid.UUID(p["campaign_id"]),
            p["definition"],
            expected_hash=p.get("expected_hash"),
            changes=p.get("changes"),
        )
        return {"campaign_id": str(c.id), "status": c.status.value, "base_hash": c.definition_hash}
    raise ValidationFailed(f"Unknown undo operation '{op}'")
