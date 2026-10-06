"""AI application tools (spec §32–34): strict schemas, server-side execution, workspace authorization,
audit logging, structured errors, confirmation for destructive actions, undo payloads. No SQL is exposed."""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from scout.auth.context import WorkspaceContext
from scout.chat.context import UIContext
from scout.db.engine import session_scope
from scout.db.enums import ActorType, CampaignStatus, EmailStatus, EntityType, ExclusionMode, SuppressionReason
from scout.db.models import Campaign, CustomColumn, Email, List, Person
from scout.errors import AppError, NotFound, ValidationFailed
from scout.jobs import queue
from scout.query.filters import FilterCondition, FilterGroup, Operator, SortSpec
from scout.query.rows import RowScope, field_registry, load_columns, query_rows
from scout.services import audit
from scout.services import lists as lists_svc
from scout.services.lists import RowRef

# =============================================================================================
# AI-friendly argument types (non-recursive so they map cleanly to function declarations)
# =============================================================================================


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Cond(Strict):
    field: str = Field(description="Field key (e.g. email_status, icp_score, title, company, city, role_family) or a custom column name (e.g. 'ManyChat')")
    operator: Operator
    value: str | float | bool | list[str] | None = None


class SimpleFilter(Strict):
    match: Literal["all", "any"] = "all"
    conditions: list[Cond] = Field(default_factory=list)


class Rows(Strict):
    target: Literal["selection", "filter", "current_view", "all", "ids"] = Field(
        description="selection = rows selected in the table ('these', 'those'); current_view = rows matching the visible "
        "filters; filter = rows matching `filter`; all = every row in the list/scope; ids = explicit ids"
    )
    filter: SimpleFilter | None = None
    ids: list[uuid.UUID] | None = None
    list_id: uuid.UUID | None = Field(default=None, description="Scope list; defaults to the current list")


@dataclass
class ToolContext:
    ws: WorkspaceContext
    ui: UIContext
    thread_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    action_id: uuid.UUID | None = None
    confirmed: bool = False


@dataclass
class ToolOutcome:
    result: dict[str, Any]                       # compact JSON fed back to the model
    card: dict[str, Any] | None = None           # compact UI card
    ui_effects: list[dict[str, Any]] = field(default_factory=list)
    audit_id: int | None = None
    undo: dict[str, Any] | None = None


@dataclass
class ToolDef:
    name: str
    description: str
    args: type[BaseModel]
    handler: Callable[[Any, ToolContext], Awaitable[ToolOutcome]]
    title: str
    needs_confirmation: Callable[[Any, ToolContext], Awaitable[str | None]] | None = None


TOOLS: dict[str, ToolDef] = {}


def tool(name: str, title: str, description: str, args: type[BaseModel], confirm: Callable[[Any, ToolContext], Awaitable[str | None]] | None = None):
    def deco(fn: Callable[[Any, ToolContext], Awaitable[ToolOutcome]]) -> Callable[[Any, ToolContext], Awaitable[ToolOutcome]]:
        TOOLS[name] = ToolDef(name=name, description=description, args=args, handler=fn, title=title, needs_confirmation=confirm)
        return fn

    return deco


async def _always(_: Any, __: ToolContext) -> str:
    return "This action cannot be undone."


# =============================================================================================
# Helpers
# =============================================================================================


async def _registry_for(ctx: ToolContext, list_id: uuid.UUID | None, entity_type: EntityType) -> tuple[dict[str, Any], list[CustomColumn]]:
    async with session_scope() as s:
        cols = await load_columns(s, ctx.ws.workspace_id, list_id)
    return field_registry(entity_type, cols), cols


FIELD_SYNONYMS = {
    "score": "icp_score", "icp": "icp_score", "icp score": "icp_score", "email status": "email_status",
    "status": "email_status", "name": "full_name", "person": "full_name", "job title": "title", "role": "role_family",
    "size": "employee_count", "employees": "employee_count", "company size": "employee_count", "confidence": "overall_confidence",
    "website": "website", "domain": "domain", "location": "city",
}


def resolve_field(raw: str, reg: dict[str, Any], cols: list[CustomColumn]) -> str:
    key = raw.strip()
    if key in reg:
        return key
    low = key.lower().strip()
    if low in reg:
        return low
    if low in FIELD_SYNONYMS and FIELD_SYNONYMS[low] in reg:
        return FIELD_SYNONYMS[low]
    for c in cols:
        if c.name.lower() == low or c.slug == low.replace(" ", "_"):
            return f"cf:{c.id}"
    for k, f in reg.items():
        if f.label.lower() == low:
            return k
    for c in cols:
        if low in c.name.lower():
            return f"cf:{c.id}"
    raise ValidationFailed(f"Unknown field or column '{raw}'", hint="Known: " + ", ".join(sorted(k for k in reg if not k.startswith("cf:"))[:30]) + "; columns: " + ", ".join(c.name for c in cols))


def to_filter_group(sf: SimpleFilter | None, reg: dict[str, Any], cols: list[CustomColumn]) -> FilterGroup | None:
    if sf is None or not sf.conditions:
        return None
    conds: list[FilterCondition | FilterGroup] = []
    for c in sf.conditions:
        key = resolve_field(c.field, reg, cols)
        f = reg[key]
        op, val = c.operator, c.value
        if f.type == "boolean" and op in ("eq", "neq") and isinstance(val, bool | str):
            truthy = val if isinstance(val, bool) else str(val).lower() in ("true", "yes", "1", "oui")
            op = "is_true" if (truthy and op == "eq") or (not truthy and op == "neq") else "is_false"
            val = None
        if f.type == "enum" and key == "email_status" and isinstance(val, str):
            val = val.upper().replace("-", "_")
        if f.type == "enum" and key == "email_status" and isinstance(val, list):
            val = [str(v).upper().replace("-", "_") for v in val]
        conds.append(FilterCondition(field=key, operator=op, value=val))
    return FilterGroup(op="and" if sf.match == "all" else "or", conditions=conds)


async def rows_to_ref(rows: Rows, ctx: ToolContext) -> RowRef:
    list_id = rows.list_id or ctx.ui.list_id
    entity = ctx.ui.entity_type
    if rows.target == "selection":
        if not ctx.ui.selected_ids:
            raise ValidationFailed("No rows are selected in the table", hint="Ask the user to select rows, or use a filter")
        return RowRef(ids=list(ctx.ui.selected_ids), entity_type=entity)
    if rows.target == "ids":
        return RowRef(ids=rows.ids or [], entity_type=entity)
    reg, cols = await _registry_for(ctx, list_id, entity)
    if rows.target == "current_view":
        fg = FilterGroup.model_validate(ctx.ui.filters) if ctx.ui.filters else None
        return RowRef(filter=fg, list_id=list_id, entity_type=entity)
    if rows.target == "filter":
        return RowRef(filter=to_filter_group(rows.filter, reg, cols), list_id=list_id, entity_type=entity)
    return RowRef(list_id=list_id, entity_type=entity)


async def _resolve(ctx: ToolContext, rows: Rows) -> lists_svc.ResolvedRows:
    ref = await rows_to_ref(rows, ctx)
    async with session_scope() as s:
        return await lists_svc.resolve_rows(s, ctx.ws.workspace_id, ref, current_selection=ctx.ui.selected_ids)


async def _list_by_ref(ctx: ToolContext, list_id: uuid.UUID | None, list_name: str | None) -> List:
    async with session_scope() as s:
        if list_id:
            return await lists_svc.get_list(s, ctx.ws.workspace_id, list_id)
        if list_name:
            lst = await lists_svc.find_list_by_name(s, ctx.ws.workspace_id, list_name)
            if lst is None:
                raise NotFound(f'No list named "{list_name}"')
            return lst
        if ctx.ui.list_id:
            return await lists_svc.get_list(s, ctx.ws.workspace_id, ctx.ui.list_id)
    raise ValidationFailed("Which list? Provide list_id or list_name")


async def _column_by_ref(ctx: ToolContext, column_id: uuid.UUID | None, column_name: str | None) -> CustomColumn:
    async with session_scope() as s:
        if column_id:
            col = await s.get(CustomColumn, column_id)
            if col and col.workspace_id == ctx.ws.workspace_id:
                return col
        if column_name:
            cols = await load_columns(s, ctx.ws.workspace_id, ctx.ui.list_id)
            for c in cols:
                if c.name.lower() == column_name.lower().strip():
                    return c
            for c in cols:
                if column_name.lower().strip() in c.name.lower():
                    return c
    raise NotFound(f"Column '{column_name or column_id}' not found")


async def _log(ctx: ToolContext, action: str, summary: str, *, entity_type: str | None = None, ids: list[Any] = (),  # type: ignore[assignment]
               undo: dict[str, Any] | None = None, campaign_id: uuid.UUID | None = None) -> int:
    async with session_scope() as s:
        e = await audit.log(s, workspace_id=ctx.ws.workspace_id, actor_id=ctx.ws.user_id, actor_type=ActorType.assistant,
                            action=action, summary=summary, entity_type=entity_type, entity_ids=ids, undo=undo,
                            assistant_action_id=ctx.action_id, campaign_id=campaign_id)
        return e.id


# =============================================================================================
# Lists
# =============================================================================================


class CreateListArgs(Strict):
    name: str = Field(min_length=1, max_length=120)
    entity_type: EntityType = EntityType.person
    description: str | None = None
    rows: Rows | None = Field(default=None, description="Optionally add these rows to the new list")


@tool("create_list", "Created list", "Create a new list (optionally filled with rows). Lists are links into the global registry.", CreateListArgs)
async def create_list(a: CreateListArgs, ctx: ToolContext) -> ToolOutcome:
    async with session_scope() as s:
        lst, created = await lists_svc.create_list(s, ctx.ws.workspace_id, name=a.name, user_id=ctx.ws.user_id,
                                                   entity_type=a.entity_type, description=a.description, if_exists="return")
        added = 0
        if a.rows:
            ref = await rows_to_ref(a.rows, ctx)
            res = await lists_svc.resolve_rows(s, ctx.ws.workspace_id, ref, current_selection=ctx.ui.selected_ids)
            change = await lists_svc.add_to_list(s, ctx.ws.workspace_id, lst.id, res.entity_type, res.ids, user_id=ctx.ws.user_id, added_via="ai")
            added = len(change.added)
        lid, lname = lst.id, lst.name
    audit_id = await _log(ctx, "list.create", f'Created list "{lname}"', entity_type="list", ids=[lid],
                          undo={"op": "set_archived", "list_id": str(lid), "archived": True} if created else None)
    return ToolOutcome(
        result={"list_id": str(lid), "name": lname, "created": created, "added": added},
        card={"kind": "list_created", "title": "Created list" if created else "List already exists", "list_id": str(lid),
              "name": lname, "added": added, "audit_id": audit_id if created else None},
        audit_id=audit_id,
    )


class ListRefArgs(Strict):
    list_id: uuid.UUID | None = None
    list_name: str | None = None


class RenameListArgs(ListRefArgs):
    new_name: str = Field(min_length=1, max_length=120)


@tool("rename_list", "Renamed list", "Rename a list.", RenameListArgs)
async def rename_list(a: RenameListArgs, ctx: ToolContext) -> ToolOutcome:
    lst = await _list_by_ref(ctx, a.list_id, a.list_name)
    async with session_scope() as s:
        _, old = await lists_svc.rename_list(s, ctx.ws.workspace_id, lst.id, a.new_name)
    audit_id = await _log(ctx, "list.rename", f'Renamed "{old}" to "{a.new_name}"', entity_type="list", ids=[lst.id],
                          undo={"op": "rename_list", "list_id": str(lst.id), "name": old})
    return ToolOutcome({"list_id": str(lst.id), "old": old, "new": a.new_name},
                       {"kind": "rows_affected", "title": "Renamed list", "detail": f"{old} → {a.new_name}", "audit_id": audit_id}, audit_id=audit_id)


@tool("archive_list", "Archived list", "Archive a list (reversible).", ListRefArgs)
async def archive_list(a: ListRefArgs, ctx: ToolContext) -> ToolOutcome:
    lst = await _list_by_ref(ctx, a.list_id, a.list_name)
    async with session_scope() as s:
        await lists_svc.set_archived(s, ctx.ws.workspace_id, lst.id, True)
    audit_id = await _log(ctx, "list.archive", f'Archived "{lst.name}"', entity_type="list", ids=[lst.id],
                          undo={"op": "set_archived", "list_id": str(lst.id), "archived": False})
    return ToolOutcome({"archived": str(lst.id)}, {"kind": "rows_affected", "title": "Archived list", "detail": lst.name, "audit_id": audit_id}, audit_id=audit_id)


async def _confirm_delete_list(a: ListRefArgs, ctx: ToolContext) -> str | None:
    lst = await _list_by_ref(ctx, a.list_id, a.list_name)
    return f'Delete list "{lst.name}"? Its memberships are removed; discovery history is kept.'


@tool("delete_list", "Deleted list", "Delete a list (memberships only; global registry and history are kept). Requires confirmation.", ListRefArgs, confirm=_confirm_delete_list)
async def delete_list(a: ListRefArgs, ctx: ToolContext) -> ToolOutcome:
    lst = await _list_by_ref(ctx, a.list_id, a.list_name)
    async with session_scope() as s:
        n = await lists_svc.delete_list(s, ctx.ws.workspace_id, lst.id)
    audit_id = await _log(ctx, "list.delete", f'Deleted "{lst.name}" ({n} memberships, history kept)', entity_type="list", ids=[lst.id])
    return ToolOutcome({"deleted": str(lst.id), "memberships_removed": n},
                       {"kind": "rows_affected", "title": "Deleted list", "detail": f"{lst.name} · history kept"},
                       ui_effects=[{"type": "list_deleted", "list_id": str(lst.id)}], audit_id=audit_id)


class DuplicateListArgs(ListRefArgs):
    new_name: str | None = None


@tool("duplicate_list", "Duplicated list", "Duplicate a list with its memberships, list columns and views.", DuplicateListArgs)
async def duplicate_list(a: DuplicateListArgs, ctx: ToolContext) -> ToolOutcome:
    lst = await _list_by_ref(ctx, a.list_id, a.list_name)
    async with session_scope() as s:
        dst = await lists_svc.duplicate_list(s, ctx.ws.workspace_id, lst.id, name=a.new_name, user_id=ctx.ws.user_id)
        did, dname = dst.id, dst.name
    audit_id = await _log(ctx, "list.duplicate", f'Duplicated "{lst.name}" as "{dname}"', entity_type="list", ids=[did],
                          undo={"op": "set_archived", "list_id": str(did), "archived": True})
    return ToolOutcome({"list_id": str(did), "name": dname},
                       {"kind": "list_created", "title": "Duplicated list", "list_id": str(did), "name": dname, "audit_id": audit_id},
                       audit_id=audit_id)


class GetListsArgs(Strict):
    query: str | None = None
    include_archived: bool = False


@tool("get_lists", "Lists", "List the workspace's lists with lead counts.", GetListsArgs)
async def get_lists(a: GetListsArgs, ctx: ToolContext) -> ToolOutcome:
    async with session_scope() as s:
        rows = await lists_svc.list_overview(s, ctx.ws.workspace_id, include_archived=a.include_archived)
    if a.query:
        rows = [r for r in rows if a.query.lower() in r["name"].lower()]
    return ToolOutcome({"lists": [{"id": str(r["id"]), "name": r["name"], "count": r["count"], "type": r["entity_type"]} for r in rows[:50]]})


@tool("get_list", "List", "Get one list with its quality summary (counts, SAFE emails, boolean column totals).", ListRefArgs)
async def get_list(a: ListRefArgs, ctx: ToolContext) -> ToolOutcome:
    lst = await _list_by_ref(ctx, a.list_id, a.list_name)
    async with session_scope() as s:
        summary = await lists_svc.quality_summary(s, ctx.ws.workspace_id, lst.id)
    return ToolOutcome({"list_id": str(lst.id), "name": lst.name, "summary": _jsonable(summary)})


class OpenListArgs(ListRefArgs):
    pass


@tool("open_list", "Opened list", "Navigate the table to a list.", OpenListArgs)
async def open_list(a: OpenListArgs, ctx: ToolContext) -> ToolOutcome:
    lst = await _list_by_ref(ctx, a.list_id, a.list_name)
    return ToolOutcome({"opened": str(lst.id)}, ui_effects=[{"type": "open_list", "list_id": str(lst.id)}])


# =============================================================================================
# Memberships
# =============================================================================================


class AddToListArgs(Strict):
    rows: Rows
    list_id: uuid.UUID | None = None
    list_name: str | None = None
    create_if_missing: bool = True


@tool("add_to_list", "Added to list", "Add rows to a list (creates the list by name if needed).", AddToListArgs)
async def add_to_list(a: AddToListArgs, ctx: ToolContext) -> ToolOutcome:
    res = await _resolve(ctx, a.rows)
    async with session_scope() as s:
        if a.list_id:
            lst = await lists_svc.get_list(s, ctx.ws.workspace_id, a.list_id)
        elif a.list_name:
            lst = await lists_svc.find_list_by_name(s, ctx.ws.workspace_id, a.list_name)
            if lst is None:
                if not a.create_if_missing:
                    raise NotFound(f'No list named "{a.list_name}"')
                lst, _ = await lists_svc.create_list(s, ctx.ws.workspace_id, name=a.list_name, user_id=ctx.ws.user_id,
                                                     entity_type=res.entity_type)
        else:
            raise ValidationFailed("Provide list_id or list_name")
        change = await lists_svc.add_to_list(s, ctx.ws.workspace_id, lst.id, res.entity_type, res.ids, user_id=ctx.ws.user_id, added_via="ai")
        lid, lname = lst.id, lst.name
    audit_id = await _log(ctx, "list.add", f'Added {len(change.added)} leads to "{lname}"', entity_type=res.entity_type.value,
                          ids=change.added, undo={"op": "remove_from_list", "list_id": str(lid), "entity_type": res.entity_type.value,
                                                  "ids": [str(i) for i in change.added]})
    detail = f"{len(change.added):,} added"
    if change.already_present:
        detail += f" · {change.already_present:,} already there"
    if change.skipped_suppressed:
        detail += f" · {change.skipped_suppressed:,} suppressed skipped"
    return ToolOutcome({"list_id": str(lid), "list": lname, "added": len(change.added), "already_present": change.already_present,
                        "skipped_suppressed": change.skipped_suppressed},
                       {"kind": "rows_affected", "title": f"Added to {lname}", "detail": detail, "list_id": str(lid), "audit_id": audit_id},
                       audit_id=audit_id)


class RemoveArgs(Strict):
    rows: Rows
    list_id: uuid.UUID | None = None
    list_name: str | None = None


async def _confirm_remove(a: RemoveArgs, ctx: ToolContext) -> str | None:
    res = await _resolve(ctx, a.rows)
    if len(res.ids) > 50:
        return f"Remove {len(res.ids):,} leads from the list? (Discovery history is kept; undo available.)"
    return None


@tool("remove_from_list", "Removed from list", "Remove rows from a list. Never erases global discovery history.", RemoveArgs, confirm=_confirm_remove)
async def remove_from_list(a: RemoveArgs, ctx: ToolContext) -> ToolOutcome:
    lst = await _list_by_ref(ctx, a.list_id, a.list_name)
    res = await _resolve(ctx, a.rows)
    async with session_scope() as s:
        removed = await lists_svc.remove_from_list(s, ctx.ws.workspace_id, lst.id, res.entity_type, res.ids)
    audit_id = await _log(ctx, "list.remove", f'Removed {len(removed)} leads from "{lst.name}"', entity_type=res.entity_type.value,
                          ids=removed, undo={"op": "add_to_list", "list_id": str(lst.id), "entity_type": res.entity_type.value,
                                             "ids": [str(i) for i in removed]})
    return ToolOutcome({"removed": len(removed)},
                       {"kind": "rows_affected", "title": f"Removed from {lst.name}", "detail": f"{len(removed):,} removed · history kept", "audit_id": audit_id},
                       ui_effects=[{"type": "refresh"}], audit_id=audit_id)


class MoveArgs(Strict):
    rows: Rows
    to_list_id: uuid.UUID | None = None
    to_list_name: str | None = None
    from_list_id: uuid.UUID | None = None


@tool("move_between_lists", "Moved leads", "Move rows from the current (or given) list to another list.", MoveArgs)
async def move_between_lists(a: MoveArgs, ctx: ToolContext) -> ToolOutcome:
    src = await _list_by_ref(ctx, a.from_list_id, None)
    res = await _resolve(ctx, a.rows)
    async with session_scope() as s:
        if a.to_list_id:
            dst = await lists_svc.get_list(s, ctx.ws.workspace_id, a.to_list_id)
        elif a.to_list_name:
            dst = await lists_svc.find_list_by_name(s, ctx.ws.workspace_id, a.to_list_name)
            if dst is None:
                dst, _ = await lists_svc.create_list(s, ctx.ws.workspace_id, name=a.to_list_name, user_id=ctx.ws.user_id, entity_type=res.entity_type)
        else:
            raise ValidationFailed("Provide the destination list")
        change, removed = await lists_svc.move_between_lists(s, ctx.ws.workspace_id, src.id, dst.id, res.entity_type, res.ids, user_id=ctx.ws.user_id)
        dname, did = dst.name, dst.id
    audit_id = await _log(ctx, "list.move", f'Moved {len(removed)} leads to "{dname}"', entity_type=res.entity_type.value, ids=removed,
                          undo={"op": "move", "from_list_id": str(src.id), "to_list_id": str(did), "entity_type": res.entity_type.value,
                                "ids": [str(i) for i in removed], "added_ids": [str(i) for i in change.added]})
    return ToolOutcome({"moved": len(removed), "to": dname},
                       {"kind": "rows_affected", "title": f"Moved to {dname}", "detail": f"{len(removed):,} leads", "list_id": str(did), "audit_id": audit_id},
                       ui_effects=[{"type": "refresh"}], audit_id=audit_id)


# =============================================================================================
# Table manipulation (UI effects)
# =============================================================================================


class FilterTableArgs(Strict):
    filter: SimpleFilter
    mode: Literal["replace", "add"] = "replace"


@tool("filter_table", "Filtered table", "Filter the visible table (non-destructive). E.g. only SAFE emails, ManyChat = true, score > 85.", FilterTableArgs)
async def filter_table(a: FilterTableArgs, ctx: ToolContext) -> ToolOutcome:
    reg, cols = await _registry_for(ctx, ctx.ui.list_id, ctx.ui.entity_type)
    fg = to_filter_group(a.filter, reg, cols) or FilterGroup()
    if a.mode == "add" and ctx.ui.filters:
        prev = FilterGroup.model_validate(ctx.ui.filters)
        fg = FilterGroup(op="and", conditions=[*prev.conditions, *fg.conditions])
    scope = RowScope(kind="list" if ctx.ui.list_id else ("people" if ctx.ui.entity_type == EntityType.person else "companies"),
                     entity_type=ctx.ui.entity_type, list_id=ctx.ui.list_id)
    async with session_scope() as s:
        res = await query_rows(s, ctx.ws.workspace_id, scope, filters=fg, limit=1)
    labels = [f"{reg[c.field].label} {c.operator.replace('_', ' ')} {'' if c.value is None else c.value}".strip() for c in fg.conditions if isinstance(c, FilterCondition)]
    return ToolOutcome({"matching_rows": res.total, "filters": labels},
                       {"kind": "filter_applied", "title": "Filtered", "detail": " · ".join(labels) + f" — {res.total:,} rows"},
                       ui_effects=[{"type": "set_filters", "filters": fg.model_dump(mode="json")}])


class SortTableArgs(Strict):
    sort: list[SortSpec]


@tool("sort_table", "Sorted table", "Sort the visible table.", SortTableArgs)
async def sort_table(a: SortTableArgs, ctx: ToolContext) -> ToolOutcome:
    reg, cols = await _registry_for(ctx, ctx.ui.list_id, ctx.ui.entity_type)
    sort = [SortSpec(field=resolve_field(x.field, reg, cols), direction=x.direction) for x in a.sort]
    return ToolOutcome({"sort": [x.model_dump() for x in sort]}, None,
                       ui_effects=[{"type": "set_sort", "sort": [x.model_dump() for x in sort]}])


class SelectRowsArgs(Strict):
    rows: Rows


@tool("select_rows", "Selected rows", "Select rows in the table.", SelectRowsArgs)
async def select_rows(a: SelectRowsArgs, ctx: ToolContext) -> ToolOutcome:
    res = await _resolve(ctx, a.rows)
    return ToolOutcome({"selected": len(res.ids)}, ui_effects=[{"type": "select_rows", "ids": [str(i) for i in res.ids[:10000]]}])


class HideColumnArgs(Strict):
    column: str = Field(description="Column key or name")
    hidden: bool = True


@tool("hide_column", "Column visibility", "Hide or show a column in the current view.", HideColumnArgs)
async def hide_column(a: HideColumnArgs, ctx: ToolContext) -> ToolOutcome:
    reg, cols = await _registry_for(ctx, ctx.ui.list_id, ctx.ui.entity_type)
    key = resolve_field(a.column, reg, cols)
    return ToolOutcome({"column": key, "hidden": a.hidden}, ui_effects=[{"type": "column_visibility", "column": key, "visible": not a.hidden}])


class SaveViewArgs(Strict):
    name: str = Field(min_length=1, max_length=80)
    filter: SimpleFilter | None = Field(default=None, description="Defaults to the current table filters")


@tool("create_saved_view", "Saved view", "Save the current table layout/filters as a named view.", SaveViewArgs)
async def create_saved_view(a: SaveViewArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.services import views as views_svc

    reg, cols = await _registry_for(ctx, ctx.ui.list_id, ctx.ui.entity_type)
    fg = to_filter_group(a.filter, reg, cols) if a.filter else (FilterGroup.model_validate(ctx.ui.filters) if ctx.ui.filters else FilterGroup())
    async with session_scope() as s:
        v = await views_svc.create_view(
            s, ctx.ws.workspace_id, name=a.name, list_id=ctx.ui.list_id, entity_type=ctx.ui.entity_type,
            layout=views_svc.ViewLayout(filters=fg or FilterGroup(), sort=[SortSpec.model_validate(x) for x in ctx.ui.sort]),
            user_id=ctx.ws.user_id,
        )
        vid = v.id
    return ToolOutcome({"view_id": str(vid), "name": a.name}, {"kind": "rows_affected", "title": "Saved view", "detail": a.name},
                       ui_effects=[{"type": "open_view", "view_id": str(vid)}])


# =============================================================================================
# Columns & enrichment
# =============================================================================================


class CreateColumnArgs(Strict):
    name: str = Field(min_length=1, max_length=80, description="Column name shown in the table")
    instruction: str = Field(description="What the column should contain, in natural language")
    scope: Literal["list", "workspace"] = "list"
    run: bool = True


@tool("create_column", "Created column", "Create ANY enrichment column from natural language; the planner picks the cheapest reliable resolver and enrichment starts.", CreateColumnArgs)
async def create_column(a: CreateColumnArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.enrich.engine import column_entity_ids, enqueue_column, estimate_coverage  # type: ignore[import-not-found]
    from scout.enrich.engine import create_column as _create  # type: ignore[import-not-found]
    from scout.enrich.planner import describe_plan  # type: ignore[import-not-found]
    from scout.enrich.types import EnrichmentPlan

    list_id = ctx.ui.list_id if a.scope == "list" else None
    col = await _create(ctx.ws.workspace_id, name=a.name, instruction=a.instruction, list_id=list_id, created_by=ctx.ws.user_id)
    plan = EnrichmentPlan.model_validate(col.configuration)
    desc = describe_plan(plan)
    async with session_scope() as s:
        col_db = await s.get(CustomColumn, col.id)
        ids = await column_entity_ids(ctx.ws.workspace_id, col_db, list_id=ctx.ui.list_id)
        coverage = await estimate_coverage(ctx.ws.workspace_id, col_db, ids)
    queued = await enqueue_column(ctx.ws.workspace_id, col.id, entity_ids=ids, list_id=ctx.ui.list_id) if a.run else 0
    audit_id = await _log(ctx, "column.create", f'Created column "{col.name}"', entity_type="column", ids=[col.id],
                          undo={"op": "delete_column", "column_id": str(col.id)})
    return ToolOutcome(
        {"column_id": str(col.id), "name": col.name, "resolver": desc.get("resolver_label"), "kind": plan.kind.value,
         "queued": queued, "coverage": coverage},
        {"kind": "column_created", "title": f"Created “{col.name}”", "column_id": str(col.id), "describe": desc,
         "coverage": coverage, "queued": queued, "audit_id": audit_id},
        ui_effects=[{"type": "refresh_columns"}], audit_id=audit_id,
    )


class ColumnRefArgs(Strict):
    column_id: uuid.UUID | None = None
    column_name: str | None = None


class RenameColumnArgs(ColumnRefArgs):
    new_name: str = Field(min_length=1, max_length=80)


@tool("rename_column", "Renamed column", "Rename a custom column.", RenameColumnArgs)
async def rename_column(a: RenameColumnArgs, ctx: ToolContext) -> ToolOutcome:
    col = await _column_by_ref(ctx, a.column_id, a.column_name)
    async with session_scope() as s:
        c = await s.get(CustomColumn, col.id)
        assert c is not None
        old = c.name
        c.name = a.new_name
    await _log(ctx, "column.rename", f'Renamed column "{old}" to "{a.new_name}"', entity_type="column", ids=[col.id])
    return ToolOutcome({"column_id": str(col.id), "name": a.new_name}, {"kind": "rows_affected", "title": "Renamed column", "detail": f"{old} → {a.new_name}"},
                       ui_effects=[{"type": "refresh_columns"}])


async def _confirm_delete_column(a: ColumnRefArgs, ctx: ToolContext) -> str | None:
    col = await _column_by_ref(ctx, a.column_id, a.column_name)
    return f'Delete column "{col.name}" and its values?'


@tool("delete_column", "Deleted column", "Delete a custom column and its values. Requires confirmation.", ColumnRefArgs, confirm=_confirm_delete_column)
async def delete_column(a: ColumnRefArgs, ctx: ToolContext) -> ToolOutcome:
    col = await _column_by_ref(ctx, a.column_id, a.column_name)
    async with session_scope() as s:
        c = await s.get(CustomColumn, col.id)
        if c:
            await s.delete(c)
    await _log(ctx, "column.delete", f'Deleted column "{col.name}"', entity_type="column", ids=[col.id])
    return ToolOutcome({"deleted": str(col.id)}, {"kind": "rows_affected", "title": "Deleted column", "detail": col.name},
                       ui_effects=[{"type": "refresh_columns"}])


class EnrichColumnArgs(ColumnRefArgs):
    rows: Rows | None = None
    only_missing: bool = True


@tool("enrich_column", "Enriching column", "Run enrichment for a column (missing cells by default).", EnrichColumnArgs)
async def enrich_column(a: EnrichColumnArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.enrich.engine import column_entity_ids, enqueue_column  # type: ignore[import-not-found]

    col = await _column_by_ref(ctx, a.column_id, a.column_name)
    person_ids = company_ids = None
    if a.rows:
        res = await _resolve(ctx, a.rows)
        person_ids, company_ids = (res.ids, None) if res.entity_type == EntityType.person else (None, res.ids)
    async with session_scope() as s:
        c = await s.get(CustomColumn, col.id)
        ids = await column_entity_ids(ctx.ws.workspace_id, c, list_id=ctx.ui.list_id, person_ids=person_ids, company_ids=company_ids)
    queued = await enqueue_column(ctx.ws.workspace_id, col.id, entity_ids=ids, only_missing=a.only_missing)
    return ToolOutcome({"column": col.name, "queued": queued},
                       {"kind": "enrichment_progress", "title": col.name, "column_id": str(col.id), "queued": queued})


class RefreshColumnArgs(ColumnRefArgs):
    older_than_days: int | None = None


@tool("refresh_column", "Refreshing column", "Re-run a column for stale (or all) cells.", RefreshColumnArgs)
async def refresh_column(a: RefreshColumnArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.enrich.engine import refresh_column as _refresh  # type: ignore[import-not-found]

    col = await _column_by_ref(ctx, a.column_id, a.column_name)
    n = await _refresh(ctx.ws.workspace_id, col.id, older_than_days=a.older_than_days)
    return ToolOutcome({"column": col.name, "queued": n}, {"kind": "enrichment_progress", "title": col.name, "column_id": str(col.id), "queued": n})


class UpdateCellArgs(Strict):
    entity_id: uuid.UUID
    field: str = Field(description="Built-in field (full_name, job_title, email, phone, website_url, description) or a custom column name")
    value: str | float | bool | None


@tool("update_cell", "Updated cell", "Edit one value (stored as a user-confirmed observation).", UpdateCellArgs)
async def update_cell(a: UpdateCellArgs, ctx: ToolContext) -> ToolOutcome:
    return await _update_cells([a.entity_id], a.field, a.value, ctx)


class BulkUpdateArgs(Strict):
    rows: Rows
    field: str
    value: str | float | bool | None


async def _confirm_bulk(a: BulkUpdateArgs, ctx: ToolContext) -> str | None:
    res = await _resolve(ctx, a.rows)
    return f"Set {a.field} = {a.value!r} on {len(res.ids):,} rows?" if len(res.ids) > 50 else None


@tool("bulk_update_cells", "Updated cells", "Set the same value on many rows.", BulkUpdateArgs, confirm=_confirm_bulk)
async def bulk_update_cells(a: BulkUpdateArgs, ctx: ToolContext) -> ToolOutcome:
    res = await _resolve(ctx, a.rows)
    return await _update_cells(res.ids, a.field, a.value, ctx)


async def _update_cells(ids: list[uuid.UUID], fld: str, value: Any, ctx: ToolContext) -> ToolOutcome:
    from scout.services.leads import EDITABLE_COMPANY_FIELDS, EDITABLE_PERSON_FIELDS, edit_field

    et = ctx.ui.entity_type
    builtin = EDITABLE_PERSON_FIELDS | {"email"} if et == EntityType.person else EDITABLE_COMPANY_FIELDS
    if fld in builtin:
        async with session_scope() as s:
            for i in ids:
                await edit_field(s, ctx.ws.workspace_id, et, i, fld, value, user_id=ctx.ws.user_id)
        await _log(ctx, "cells.edit", f"Edited {fld} on {len(ids)} rows", entity_type=et.value, ids=ids)
        return ToolOutcome({"updated": len(ids)}, {"kind": "rows_affected", "title": "Updated", "detail": f"{fld} on {len(ids):,} rows"},
                           ui_effects=[{"type": "refresh"}])
    from scout.enrich.engine import set_user_value  # type: ignore[import-not-found]

    col = await _column_by_ref(ctx, None, fld)
    targets = ids
    if col.entity_type == EntityType.company and et == EntityType.person:
        async with session_scope() as s:
            targets = list({c for c in (await s.scalars(sa.select(Person.company_id).where(Person.id.in_(ids)))).all() if c})
    for t in targets:
        await set_user_value(ctx.ws.workspace_id, col.id, col.entity_type, t, value, user_id=ctx.ws.user_id)
    await _log(ctx, "cells.edit", f'Set "{col.name}" on {len(targets)} rows', entity_type=col.entity_type.value, ids=targets)
    return ToolOutcome({"updated": len(targets)}, {"kind": "rows_affected", "title": f"Updated {col.name}", "detail": f"{len(targets):,} rows"},
                       ui_effects=[{"type": "refresh"}])


# =============================================================================================
# Campaigns
# =============================================================================================


class CreateCampaignArgs(Strict):
    request: str = Field(description="The user's lead request in their own words (the ICP parser handles it)")
    target_list_name: str | None = Field(default=None, description="Existing or new list to receive leads")


@tool("create_campaign", "Campaign started", "Start a lead discovery campaign from a natural-language ICP. Continues until the number of QUALIFIED leads is reached.", CreateCampaignArgs)
async def create_campaign(a: CreateCampaignArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.api.routes_campaigns import build_parse_context
    from scout.pipeline import campaigns as csvc
    from scout.pipeline.icp import parse_prompt

    pc = await build_parse_context(ctx.ws, ctx.ui.list_id, ctx.ui.selected_ids)
    defn, parser = await parse_prompt(a.request, pc)
    async with session_scope() as s:
        target_list_id = None
        if a.target_list_name:
            lst, _ = await lists_svc.create_list(s, ctx.ws.workspace_id, name=a.target_list_name, user_id=ctx.ws.user_id,
                                                 entity_type=EntityType.company if defn.mode.value == "companies" else EntityType.person,
                                                 if_exists="return")
            target_list_id = lst.id
        c = await csvc.create_campaign(s, ctx.ws.workspace_id, defn, user_id=ctx.ws.user_id, prompt=a.request, target_list_id=target_list_id)
        cid, interp, tl = c.id, c.interpretation, c.target_list_id
    await _log(ctx, "campaign.create", f'Started campaign "{defn.name or a.request[:40]}"', entity_type="campaign", ids=[cid], campaign_id=cid)
    return ToolOutcome(
        {"campaign_id": str(cid), "target": defn.target_qualified_count, "list_id": str(tl), "parser": parser,
         "interpretation": interp, "exclusion": defn.exclusion.mode.value},
        {"kind": "campaign_started", "title": "Campaign created", "campaign_id": str(cid), "interpretation": interp,
         "target": defn.target_qualified_count, "list_id": str(tl)},
        ui_effects=[{"type": "open_list", "list_id": str(tl)}],
    )


class FindMoreArgs(Strict):
    count: int = Field(ge=1, le=100000)
    like: Rows | None = Field(default=None, description="Reference leads; defaults to the current selection or list")
    extra_request: str | None = Field(default=None, description="Additional constraints in natural language")
    exclude_existing_companies: bool = False


@tool("find_more_leads", "Finding more leads", "Find N more leads like these (profile from reference leads), never repeating anyone.", FindMoreArgs)
async def find_more_leads(a: FindMoreArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.api.routes_campaigns import build_parse_context
    from scout.pipeline import campaigns as csvc
    from scout.pipeline.icp import parse_prompt
    from scout.schemas.campaign import CampaignDefinition

    rows = a.like or Rows(target="selection" if ctx.ui.selected_ids else "all")
    res = await _resolve(ctx, rows)
    profile = await _lead_profile(ctx, res.ids[:500], res.entity_type)
    pc = await build_parse_context(ctx.ws, ctx.ui.list_id, [])
    pc.like_profile = profile
    request = f"Find {a.count} new leads like these. Don't repeat anyone. {a.extra_request or ''}"
    defn, _ = await parse_prompt(request, pc)
    # reuse the source campaign's definition when the list came from a campaign
    base = profile.get("campaign_definition")
    if base:
        d2 = CampaignDefinition.model_validate(base)
        d2.target_qualified_count = a.count
        d2.exclusion = defn.exclusion
        d2.seed = defn.seed
        defn = d2
    defn.exclusion.mode = ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE_AND_COMPANIES if a.exclude_existing_companies else ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE
    defn.exclusion.previous_people = True
    defn.exclusion.previous_companies = a.exclude_existing_companies
    defn.target_qualified_count = a.count
    async with session_scope() as s:
        c = await csvc.create_campaign(s, ctx.ws.workspace_id, defn, user_id=ctx.ws.user_id, prompt=request,
                                       target_list_id=ctx.ui.list_id, parent_campaign_id=profile.get("campaign_id"))
        cid, interp = c.id, c.interpretation
    return ToolOutcome({"campaign_id": str(cid), "target": a.count, "interpretation": interp},
                       {"kind": "campaign_started", "title": f"Finding {a.count:,} more like these", "campaign_id": str(cid),
                        "interpretation": interp, "target": a.count, "list_id": str(ctx.ui.list_id) if ctx.ui.list_id else None})


async def _lead_profile(ctx: ToolContext, ids: list[uuid.UUID], et: EntityType) -> dict[str, Any]:
    from scout.db.models import Company

    async with session_scope() as s:
        if et == EntityType.person:
            rows = (await s.execute(sa.select(Company.industry, Company.country, Company.city, Company.employee_min, Company.employee_max, Person.normalized_title)
                                    .join(Company, Company.id == Person.company_id).where(Person.id.in_(ids)))).all()
        else:
            rows = [(r[0], r[1], r[2], r[3], r[4], None) for r in (await s.execute(sa.select(Company.industry, Company.country, Company.city, Company.employee_min, Company.employee_max).where(Company.id.in_(ids)))).all()]
        camp = None
        if ctx.ui.list_id:
            camp = await s.scalar(sa.select(Campaign).where(Campaign.target_list_id == ctx.ui.list_id).order_by(Campaign.created_at.desc()).limit(1))

    def top(vals: list[Any], n: int) -> list[Any]:
        counts: dict[Any, int] = {}
        for v in vals:
            if v:
                counts[v] = counts.get(v, 0) + 1
        return [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:n]]

    mins = [r[3] for r in rows if r[3] is not None]
    maxs = [r[4] for r in rows if r[4] is not None]
    return {
        "industries": top([r[0] for r in rows], 2), "countries": top([r[1] for r in rows], 2), "cities": top([r[2] for r in rows], 5),
        "employee_min": min(mins) if mins else None, "employee_max": max(maxs) if maxs else None,
        "titles": top([r[5] for r in rows], 4), "campaign_definition": camp.definition if camp else None,
        "campaign_id": camp.id if camp else None,
    }


class FindDecisionMakersArgs(Strict):
    titles: list[str] = Field(description="e.g. ['Head of Marketing', 'Marketing Director']")
    companies: Rows | None = Field(default=None, description="Companies to search; defaults to all companies of the current list")
    max_per_company: int = Field(default=1, ge=1, le=5)
    exclude_known_people: bool = True


@tool("find_decision_makers", "Finding decision makers", "Find (additional) decision makers at existing companies without rediscovering companies.", FindDecisionMakersArgs)
async def find_decision_makers(a: FindDecisionMakersArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.pipeline import campaigns as csvc
    from scout.schemas.campaign import CampaignDefinition, ExclusionSpec, PeopleFilters, Seed

    res = await _resolve(ctx, a.companies or Rows(target="all"))
    async with session_scope() as s:
        if res.entity_type == EntityType.person:
            company_ids = list({c for c in (await s.scalars(sa.select(Person.company_id).where(Person.id.in_(res.ids)))).all() if c})
        else:
            company_ids = res.ids
    defn = CampaignDefinition(
        name=f"{' / '.join(a.titles[:2])} at existing companies",
        target_qualified_count=max(1, len(company_ids) * a.max_per_company),
        people_filters=PeopleFilters(titles=a.titles, max_people_per_company=a.max_per_company),
        exclusion=ExclusionSpec(mode=ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE if a.exclude_known_people else ExclusionMode.NONE,
                                previous_people=a.exclude_known_people, allow_new_people_at_existing_companies=True),
        seed=Seed(type="selection", company_ids=company_ids),
        minimum_company_fit=0,
    )
    async with session_scope() as s:
        c = await csvc.create_campaign(s, ctx.ws.workspace_id, defn, user_id=ctx.ws.user_id,
                                       prompt=f"Find {', '.join(a.titles)} at {len(company_ids)} existing companies", target_list_id=ctx.ui.list_id)
        cid, interp = c.id, c.interpretation
    return ToolOutcome({"campaign_id": str(cid), "companies": len(company_ids)},
                       {"kind": "campaign_started", "title": "Finding decision makers", "campaign_id": str(cid), "interpretation": interp,
                        "target": defn.target_qualified_count, "list_id": str(ctx.ui.list_id) if ctx.ui.list_id else None})


class CampaignRefArgs(Strict):
    campaign_id: uuid.UUID | None = Field(default=None, description="Defaults to the latest active campaign")


async def _campaign_ref(ctx: ToolContext, cid: uuid.UUID | None) -> uuid.UUID:
    if cid:
        return cid
    if ctx.ui.campaign_id:
        return ctx.ui.campaign_id
    async with session_scope() as s:
        found = await s.scalar(sa.select(Campaign.id).where(Campaign.workspace_id == ctx.ws.workspace_id)
                               .order_by(sa.case((Campaign.status.in_([CampaignStatus.running, CampaignStatus.paused]), 0), else_=1), Campaign.created_at.desc()).limit(1))
    if not found:
        raise NotFound("No campaign found")
    return found


@tool("get_campaign_status", "Campaign status", "Live funnel, progress, ETA and stop reason of a campaign.", CampaignRefArgs)
async def get_campaign_status(a: CampaignRefArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.pipeline import campaigns as csvc

    cid = await _campaign_ref(ctx, a.campaign_id)
    async with session_scope() as s:
        st = await csvc.campaign_status(s, ctx.ws.workspace_id, cid)
    compact = {k: st[k] for k in ("name", "status", "stop_reason", "target", "stats", "eta_minutes", "cost_usd", "top_rejections")}
    return ToolOutcome(_jsonable(compact), {"kind": "campaign_progress", "campaign_id": str(cid), "title": st["name"]})


async def _confirm_cancel(a: CampaignRefArgs, ctx: ToolContext) -> str | None:
    return "Cancel this campaign? Already qualified leads are kept."


def _lifecycle(action: str) -> Callable[[CampaignRefArgs, ToolContext], Awaitable[ToolOutcome]]:
    async def run(a: CampaignRefArgs, ctx: ToolContext) -> ToolOutcome:
        from scout.pipeline import campaigns as csvc

        cid = await _campaign_ref(ctx, a.campaign_id)
        fn = {"pause": csvc.pause_campaign, "resume": csvc.resume_campaign, "cancel": csvc.cancel_campaign}[action]
        async with session_scope() as s:
            c = await fn(s, ctx.ws.workspace_id, cid)
            name, status = c.name, c.status.value
        await _log(ctx, f"campaign.{action}", f'{action.title()} "{name}"', entity_type="campaign", ids=[cid], campaign_id=cid)
        return ToolOutcome({"campaign_id": str(cid), "status": status},
                           {"kind": "campaign_progress", "campaign_id": str(cid), "title": name})

    return run


tool("pause_campaign", "Paused campaign", "Pause a running campaign.", CampaignRefArgs)(_lifecycle("pause"))
tool("resume_campaign", "Resumed campaign", "Resume a paused campaign.", CampaignRefArgs)(_lifecycle("resume"))
tool("cancel_campaign", "Cancelled campaign", "Cancel a campaign (qualified leads are kept). Requires confirmation.", CampaignRefArgs, confirm=_confirm_cancel)(_lifecycle("cancel"))


class ExcludePreviousArgs(Strict):
    mode: ExclusionMode
    list_names: list[str] = Field(default_factory=list)
    cooldown_days: int | None = None


@tool("exclude_previous_leads", "Exclusion updated", "Explain or preview how previously seen leads will be excluded for the next campaign (exclusion rules).", ExcludePreviousArgs)
async def exclude_previous_leads(a: ExcludePreviousArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.schemas.campaign import ExclusionSpec
    from scout.services.exclusion import compile_rules

    async with session_scope() as s:
        ids = []
        for n in a.list_names:
            lst = await lists_svc.find_list_by_name(s, ctx.ws.workspace_id, n)
            if lst:
                ids.append(lst.id)
    rules = compile_rules(ExclusionSpec(mode=a.mode, list_ids=ids, cooldown_days=a.cooldown_days))
    return ToolOutcome({"rules": [r.describe() for r in rules]}, {"kind": "rows_affected", "title": "Exclusion rules", "detail": "; ".join(r.describe() for r in rules) or "none"})


# =============================================================================================
# Email
# =============================================================================================


class EmailRowsArgs(Strict):
    rows: Rows


@tool("find_emails", "Finding emails", "Find professional emails for people (waterfall: published → domain pattern → permutations → verification).", EmailRowsArgs)
async def find_emails(a: EmailRowsArgs, ctx: ToolContext) -> ToolOutcome:
    res = await _resolve(ctx, a.rows)
    if res.entity_type != EntityType.person:
        raise ValidationFailed("Email finding applies to people")
    async with session_scope() as s:
        for i in range(0, len(res.ids), 50):
            await queue.enqueue(s, workspace_id=ctx.ws.workspace_id, type="email.find", priority=8,
                                payload={"person_ids": [str(x) for x in res.ids[i:i + 50]]})
    return ToolOutcome({"queued": len(res.ids)}, {"kind": "enrichment_progress", "title": "Finding emails", "queued": len(res.ids)})


class VerifyArgs(Strict):
    rows: Rows | None = None
    statuses: list[EmailStatus] = Field(default_factory=list, description="e.g. ['RISKY'] to re-verify risky emails only")


@tool("verify_emails", "Verifying emails", "Re-verify emails (optionally only some statuses).", VerifyArgs)
async def verify_emails(a: VerifyArgs, ctx: ToolContext) -> ToolOutcome:
    res = await _resolve(ctx, a.rows or Rows(target="all"))
    async with session_scope() as s:
        q = sa.select(Email.id).where(Email.workspace_id == ctx.ws.workspace_id, Email.person_id.in_(res.ids))
        if a.statuses:
            q = q.where(Email.status.in_(a.statuses))
        email_ids = (await s.scalars(q)).all()
        for i in range(0, len(email_ids), 50):
            await queue.enqueue(s, workspace_id=ctx.ws.workspace_id, type="email.verify", priority=8,
                                payload={"email_ids": [str(x) for x in email_ids[i:i + 50]]})
    return ToolOutcome({"queued": len(email_ids)}, {"kind": "enrichment_progress", "title": "Verifying emails", "queued": len(email_ids)})


# =============================================================================================
# Export / import / suppression
# =============================================================================================


class ExportArgs(Strict):
    rows: Rows | None = Field(default=None, description="Defaults to the current view")
    columns: Literal["visible", "all"] = "visible"
    format: Literal["csv", "json"] = "csv"


@tool("export_leads", "Export ready", "Export rows to CSV/JSON (records EXPORTED so future searches can exclude them).", ExportArgs)
async def export_leads(a: ExportArgs, ctx: ToolContext) -> ToolOutcome:
    rows = a.rows or Rows(target="current_view")
    ref = await rows_to_ref(rows, ctx)
    payload: dict[str, Any] = {
        "scope": "selected" if ref.ids is not None else ("view" if ref.filter else "list"),
        "entity_type": ctx.ui.entity_type.value, "list_id": str(ctx.ui.list_id) if ctx.ui.list_id else None,
        "ids": [str(i) for i in ref.ids] if ref.ids is not None else None,
        "filters": ref.filter.model_dump(mode="json") if ref.filter else None,
        "columns": ctx.ui.visible_columns if a.columns == "visible" else None, "columns_mode": a.columns, "format": a.format,
    }
    return ToolOutcome({"export": "ready_for_download"}, {"kind": "export_ready", "title": "Export ready", "request": payload},
                       ui_effects=[{"type": "download_export", "request": payload}])


@tool("import_leads", "Import", "Open the CSV import dialog (column mapping, imported leads marked as known).", Strict)
async def import_leads(a: Strict, ctx: ToolContext) -> ToolOutcome:
    return ToolOutcome({"opened": "import_dialog"}, None, ui_effects=[{"type": "open_import"}])


class SuppressArgs(Strict):
    rows: Rows
    reason: SuppressionReason = SuppressionReason.manual


async def _confirm_suppress(a: SuppressArgs, ctx: ToolContext) -> str | None:
    res = await _resolve(ctx, a.rows)
    return f"Suppress {len(res.ids):,} leads? They will be removed from lists and never rediscovered."


@tool("suppress_leads", "Suppressed", "Suppress leads (do-not-contact / opt-out). Requires confirmation.", SuppressArgs, confirm=_confirm_suppress)
async def suppress_leads(a: SuppressArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.services.suppression import suppress

    res = await _resolve(ctx, a.rows)
    async with session_scope() as s:
        n = await suppress(s, ctx.ws.workspace_id, reason=a.reason, user_id=ctx.ws.user_id,
                           person_ids=res.ids if res.entity_type == EntityType.person else [],
                           company_ids=res.ids if res.entity_type == EntityType.company else [])
    await _log(ctx, "suppress", f"Suppressed {len(res.ids)} leads", entity_type=res.entity_type.value, ids=res.ids)
    return ToolOutcome({"suppressed": n}, {"kind": "rows_affected", "title": "Suppressed", "detail": f"{len(res.ids):,} leads"},
                       ui_effects=[{"type": "refresh"}])


# =============================================================================================
# Read / explain
# =============================================================================================


class LeadRefArgs(Strict):
    entity_id: uuid.UUID | None = Field(default=None, description="Defaults to the single selected row")
    entity_type: EntityType = EntityType.person


def _single(ctx: ToolContext, a: LeadRefArgs) -> uuid.UUID:
    if a.entity_id:
        return a.entity_id
    if len(ctx.ui.selected_ids) == 1:
        return ctx.ui.selected_ids[0]
    raise ValidationFailed("Which lead? Select exactly one row or give its id")


class SourcesArgs(LeadRefArgs):
    field: str | None = None


@tool("get_sources", "Sources", "Where a lead's data came from (field-level provenance with confidence and dates).", SourcesArgs)
async def get_sources(a: SourcesArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.services.leads import company_detail, person_detail

    eid = _single(ctx, a)
    async with session_scope() as s:
        d = await (person_detail(s, ctx.ws.workspace_id, eid) if a.entity_type == EntityType.person else company_detail(s, ctx.ws.workspace_id, eid))
    obs = d.get("observations", {})
    if a.field:
        obs = {k: v for k, v in obs.items() if a.field.lower() in k.lower()}
    compact = {k: [{"value": o["value"], "source": o["source_label"], "url": o["source_url"], "confidence": o["confidence"],
                    "observed_at": str(o["observed_at"])[:10]} for o in v[:3]] for k, v in obs.items()}
    return ToolOutcome({"sources": compact}, {"kind": "sources", "title": "Sources", "entity_id": str(eid), "entity_type": a.entity_type.value})


@tool("get_lead_history", "History", "Timeline of a lead: discovered, lists, crawls, emails, enrichments, exports, campaigns that produced it.", LeadRefArgs)
async def get_lead_history(a: LeadRefArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.services.leads import lead_campaigns, lead_history

    eid = _single(ctx, a)
    async with session_scope() as s:
        hist = await lead_history(s, ctx.ws.workspace_id, a.entity_type, eid)
        camps = await lead_campaigns(s, ctx.ws.workspace_id, a.entity_type, eid)
    first = min((h["at"] for h in hist if h.get("at")), default=None)
    return ToolOutcome({"first_seen": str(first) if first else None, "campaigns": [{"name": c["name"], "first": str(c["first_discovered_at"])} for c in camps],
                        "events": [{"at": str(h["at"])[:16], "label": h["label"], "detail": h.get("detail")} for h in hist[-25:]]},
                       {"kind": "history", "title": "History", "entity_id": str(eid), "entity_type": a.entity_type.value})


@tool("explain_score", "Score explanation", "Explain why a lead has its ICP score, with evidence (no hidden reasoning).", LeadRefArgs)
async def explain_score(a: LeadRefArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.services.leads import person_detail

    eid = _single(ctx, a)
    async with session_scope() as s:
        d = await person_detail(s, ctx.ws.workspace_id, eid)
    sc = d.get("score")
    if not sc:
        return ToolOutcome({"score": None, "explanation": ["This lead has not been scored by a campaign yet."]})
    return ToolOutcome({"icp_score": sc["icp_score"], "explanation": sc["explanation"], "components": {k: sc[k] for k in ("company_fit", "person_fit", "intent", "contactability", "evidence")},
                        "confidences": {k: sc[k] for k in ("company_confidence", "person_confidence", "email_confidence", "overall_confidence")}},
                       {"kind": "lead_summary", "title": f"{d['full_name']} · {sc['icp_score']}", "entity_id": str(eid), "bullets": sc["explanation"]})


class QueryLeadsArgs(Strict):
    filter: SimpleFilter | None = None
    limit: int = Field(default=10, ge=1, le=50)


@tool("query_leads", "Looked up leads", "Count and sample leads matching a filter in the current scope (read-only).", QueryLeadsArgs)
async def query_leads(a: QueryLeadsArgs, ctx: ToolContext) -> ToolOutcome:
    reg, cols = await _registry_for(ctx, ctx.ui.list_id, ctx.ui.entity_type)
    fg = to_filter_group(a.filter, reg, cols)
    scope = RowScope(kind="list" if ctx.ui.list_id else ("people" if ctx.ui.entity_type == EntityType.person else "companies"),
                     entity_type=ctx.ui.entity_type, list_id=ctx.ui.list_id)
    async with session_scope() as s:
        res = await query_rows(s, ctx.ws.workspace_id, scope, filters=fg, limit=a.limit)
    sample = [{k: _jsonable(r.get(k)) for k in ("full_name", "title", "company", "email", "email_status", "icp_score", "city") if k in r} for r in res.rows]
    return ToolOutcome({"total": res.total, "sample": sample})


# =============================================================================================
# Dispatch helpers
# =============================================================================================


def _jsonable(v: Any) -> Any:
    import datetime as _dt
    import decimal

    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, _dt.datetime | _dt.date):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if hasattr(v, "value") and not isinstance(v, str | int | float | bool):
        return v.value
    return v


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Inline $refs and drop titles so the schema is a self-contained function declaration."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node: Any, depth: int = 0) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return inline(dict(defs.get(name, {})), depth + 1) if depth < 8 else {"type": "object"}
            return {k: inline(v, depth) for k, v in node.items() if k not in ("title",)}
        if isinstance(node, list):
            return [inline(x, depth) for x in node]
        return node

    out = inline(schema)
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    return out


def describe_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, AppError):
        return exc.to_dict()
    return {"error": {"code": "internal", "message": re.sub(r"\s+", " ", str(exc))[:300]}}


class RerunArgs(Strict):
    campaign_id: uuid.UUID | None = None
    campaign_name: str | None = Field(default=None, description="Name of a previous campaign or saved search")
    only_new: bool = True
    count: int | None = Field(default=None, ge=1, le=100000)


@tool("rerun_campaign", "Campaign started", "Run a previous campaign or saved search again (only new leads by default; the registry excludes previous results).", RerunArgs)
async def rerun_campaign(a: RerunArgs, ctx: ToolContext) -> ToolOutcome:
    from scout.db.models import CampaignTemplate
    from scout.pipeline import campaigns as csvc
    from scout.schemas.campaign import CampaignDefinition

    async with session_scope() as s:
        src = None
        if a.campaign_id:
            src = await csvc.get_campaign(s, ctx.ws.workspace_id, a.campaign_id)
        elif a.campaign_name:
            tpl = await s.scalar(sa.select(CampaignTemplate).where(CampaignTemplate.workspace_id == ctx.ws.workspace_id,
                                                                   sa.func.lower(CampaignTemplate.name) == a.campaign_name.lower()))
            if tpl is not None:
                c = await csvc.run_template(s, ctx.ws.workspace_id, tpl.id, user_id=ctx.ws.user_id, only_new=a.only_new, target_count=a.count)
                cid, interp, tl, target = c.id, c.interpretation, c.target_list_id, c.target_qualified_count
                src = None
                return ToolOutcome({"campaign_id": str(cid), "target": target},
                                   {"kind": "campaign_started", "title": f"Running “{tpl.name}” again", "campaign_id": str(cid),
                                    "interpretation": interp, "target": target, "list_id": str(tl)})
            src = await s.scalar(sa.select(Campaign).where(Campaign.workspace_id == ctx.ws.workspace_id,
                                                           Campaign.name.ilike(f"%{a.campaign_name}%")).order_by(Campaign.created_at.desc()).limit(1))
        else:
            q = sa.select(Campaign).where(Campaign.workspace_id == ctx.ws.workspace_id)
            if ctx.ui.list_id:
                q = q.where(Campaign.target_list_id == ctx.ui.list_id)
            src = await s.scalar(q.order_by(Campaign.created_at.desc()).limit(1))
        if src is None:
            raise NotFound("No previous campaign to run again")
        defn = CampaignDefinition.model_validate(src.definition)
        if a.only_new and defn.exclusion.mode == ExclusionMode.NONE:
            defn.exclusion.mode = ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE
            defn.exclusion.previous_people = True
        if a.count:
            defn.target_qualified_count = a.count
        c = await csvc.create_campaign(s, ctx.ws.workspace_id, defn, user_id=ctx.ws.user_id, prompt=src.prompt,
                                       target_list_id=src.target_list_id, parent_campaign_id=src.id)
        cid, interp, tl, target = c.id, c.interpretation, c.target_list_id, c.target_qualified_count
    await _log(ctx, "campaign.rerun", f'Re-ran "{src.name}"', entity_type="campaign", ids=[cid], campaign_id=cid)
    return ToolOutcome({"campaign_id": str(cid), "target": target, "exclusion": defn.exclusion.mode.value},
                       {"kind": "campaign_started", "title": "Campaign started again", "campaign_id": str(cid), "interpretation": interp,
                        "target": target, "list_id": str(tl) if tl else None})
