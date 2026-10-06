"""CSV/JSON export (spec §86, §143). Increments times_exported / last_exported_at via EXPORTED exposures."""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from scout.config import get_settings
from scout.db.enums import EntityType, ExportScope, ExposureType
from scout.db.models import Export
from scout.query.filters import FilterGroup, SortSpec
from scout.query.rows import RowScope, query_rows
from scout.services import registry
from scout.util.csv_safe import safe_cell

PERSON_EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("full_name", "Full name"), ("first_name", "First name"), ("last_name", "Last name"), ("title", "Title"),
    ("company", "Company"), ("domain", "Domain"), ("website", "Website"), ("email", "Email"),
    ("email_status", "Email status"), ("email_confidence", "Email confidence"), ("profile_url", "Public profile"),
    ("phone", "Phone"), ("city", "City"), ("country", "Country"), ("industry", "Industry"),
    ("employee_min", "Employees min"), ("employee_max", "Employees max"), ("icp_score", "ICP score"),
    ("overall_confidence", "Confidence"), ("person_confidence", "Person confidence"),
    ("company_confidence", "Company confidence"), ("seniority", "Seniority"), ("role_family", "Role family"),
    ("sources", "Sources"), ("updated_at", "Updated"),
]
COMPANY_EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("company", "Company"), ("domain", "Domain"), ("website", "Website"), ("description", "Description"),
    ("phone", "Phone"), ("city", "City"), ("region", "Region"), ("country", "Country"), ("industry", "Industry"),
    ("employee_min", "Employees min"), ("employee_max", "Employees max"), ("registry_id", "Registry ID"),
    ("people_count", "People found"), ("company_confidence", "Company confidence"), ("sources", "Sources"),
    ("updated_at", "Updated"),
]


@dataclass
class ExportResult:
    export_id: uuid.UUID
    filename: str
    content_type: str
    body: bytes
    row_count: int


def _fmt(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, list):
        return "; ".join(str(x) for x in v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if hasattr(v, "value"):
        return v.value
    return v


async def export_rows(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    user_id: str | None,
    entity_type: EntityType,
    scope: ExportScope,
    list_id: uuid.UUID | None = None,
    view_id: uuid.UUID | None = None,
    ids: list[uuid.UUID] | None = None,
    filters: FilterGroup | None = None,
    sort: list[SortSpec] | None = None,
    search: str | None = None,
    columns: list[str] | None = None,
    columns_mode: Literal["visible", "all"] = "all",
    fmt: Literal["csv", "json"] = "csv",
    filename_hint: str = "scout-export",
) -> ExportResult:
    row_scope = RowScope(
        kind="list" if list_id else ("people" if entity_type == EntityType.person else "companies"),
        entity_type=entity_type,
        list_id=list_id,
    )
    use_filters = filters if scope != ExportScope.list else None
    rows: list[dict[str, Any]] = []
    cursor = None
    max_rows = get_settings().export_max_rows
    custom_cols = []
    while True:
        res = await query_rows(
            s, workspace_id, row_scope, filters=use_filters, sort=sort, search=search if scope != ExportScope.list else None,
            cursor=cursor, limit=1000, with_total=False, ids=ids if scope == ExportScope.selected else None,
        )
        custom_cols = res.columns
        rows.extend(res.rows)
        cursor = res.next_cursor
        if not cursor or len(rows) >= max_rows:
            break
    base_cols = PERSON_EXPORT_COLUMNS if entity_type == EntityType.person else COMPANY_EXPORT_COLUMNS
    all_cols: list[tuple[str, str]] = [*base_cols, *[(f"cf:{c.id}", c.name) for c in custom_cols]]
    if columns_mode == "visible" and columns:
        wanted = [c for c in columns if c not in ("select",)]
        lookup = dict(all_cols)
        all_cols = [(k, lookup.get(k, k)) for k in wanted if k in lookup]
    export = Export(
        workspace_id=workspace_id, list_id=list_id, view_id=view_id, scope=scope, columns_mode=columns_mode,
        format=fmt, row_count=len(rows), created_by=user_id,
    )
    s.add(export)
    await s.flush()
    if rows:
        if entity_type == EntityType.person:
            await registry.record_exposures(
                s, workspace_id, ExposureType.EXPORTED, person_ids=[r["id"] for r in rows], export_id=export.id, list_id=list_id
            )
        else:
            await registry.record_exposures(
                s, workspace_id, ExposureType.EXPORTED, company_ids=[r["id"] for r in rows], export_id=export.id, list_id=list_id
            )

    def value(r: dict[str, Any], key: str) -> Any:
        if key.startswith("cf:"):
            cell = (r.get("cells") or {}).get(key[3:])
            if not cell:
                return None
            return cell.get("d") if cell.get("d") is not None else cell.get("v")
        return _fmt(r.get(key))

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    if fmt == "json":
        payload = [{label: value(r, key) for key, label in all_cols} for r in rows]
        body = orjson.dumps(payload, default=str)
        return ExportResult(export.id, f"{filename_hint}-{stamp}.json", "application/json", body, len(rows))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in all_cols])
    for r in rows:
        w.writerow([safe_cell(value(r, key)) for key, _ in all_cols])
    return ExportResult(export.id, f"{filename_hint}-{stamp}.csv", "text/csv; charset=utf-8", ("﻿" + buf.getvalue()).encode(), len(rows))


def iter_csv(rows: Iterable[list[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow([safe_cell(x) for x in r])
    return buf.getvalue()
