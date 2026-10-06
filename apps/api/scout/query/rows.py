"""Table rows: people/company scopes, field registry, keyset pagination (spec §37, §142)."""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

import orjson
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import ColumnDataType, EmailStatus, EntityType, RoleFamily, Seniority
from scout.db.models import CustomColumn, CustomFieldValue
from scout.errors import ValidationFailed
from scout.query.filters import Compiler, FieldDef, FilterGroup, SortSpec

EMAIL_STATUSES = tuple(e.value for e in EmailStatus)

PERSON_FIELDS: list[FieldDef] = [
    FieldDef("full_name", "Person", "p.full_name", "text"),
    FieldDef("first_name", "First name", "p.first_name", "text"),
    FieldDef("last_name", "Last name", "p.last_name", "text"),
    FieldDef("title", "Title", "p.job_title", "text"),
    FieldDef("normalized_title", "Normalized title", "p.normalized_title", "text"),
    FieldDef("seniority", "Seniority", "p.seniority", "enum", tuple(s.value for s in Seniority)),
    FieldDef("role_family", "Role", "p.role_family", "enum", tuple(r.value for r in RoleFamily)),
    FieldDef("decision_power", "Decision power", "p.decision_power", "number"),
    FieldDef("profile_url", "Public profile", "p.public_profile_url", "text"),
    FieldDef("person_confidence", "Person confidence", "p.identity_confidence", "percent"),
    FieldDef("company", "Company", "c.name", "text"),
    FieldDef("domain", "Domain", "c.normalized_domain", "text"),
    FieldDef("website", "Website", "c.website_url", "text"),
    FieldDef("city", "City", "c.city", "text"),
    FieldDef("region", "Region", "c.region", "text"),
    FieldDef("country", "Country", "c.country", "text"),
    FieldDef("location", "Location", "concat_ws(', ', c.city, c.country)", "text"),
    FieldDef("industry", "Industry", "c.industry", "text"),
    FieldDef("employee_count", "Company size", "coalesce(c.employee_max, c.employee_min)", "number"),
    FieldDef("employee_min", "Employees (min)", "c.employee_min", "number"),
    FieldDef("employee_max", "Employees (max)", "c.employee_max", "number"),
    FieldDef("company_confidence", "Company confidence", "c.company_confidence", "percent"),
    FieldDef("phone", "Phone", "coalesce(p.phone, c.phone)", "text"),
    FieldDef("email", "Email", "e.address", "text"),
    FieldDef("email_status", "Email status", "e.status", "enum", EMAIL_STATUSES),
    FieldDef("email_confidence", "Email confidence", "e.overall_confidence", "percent"),
    FieldDef("icp_score", "ICP score", "q.icp_score", "number"),
    FieldDef("overall_confidence", "Confidence", "q.overall_confidence", "percent"),
    FieldDef("first_seen_at", "First seen", "p.first_seen_at", "date"),
    FieldDef("updated_at", "Updated", "greatest(p.updated_at, c.updated_at)", "date"),
    FieldDef("times_exported", "Times exported", "p.times_exported", "number"),
    FieldDef("last_exported_at", "Last exported", "p.last_exported_at", "date"),
    FieldDef("contacted_at", "Contacted", "p.contacted_at", "date"),
    FieldDef(
        "needs_review",
        "Needs review",
        "CASE WHEN p.needs_review OR c.needs_review THEN 'true' ELSE 'false' END",
        "boolean",
    ),
]

COMPANY_FIELDS: list[FieldDef] = [
    FieldDef("company", "Company", "c.name", "text"),
    FieldDef("domain", "Domain", "c.normalized_domain", "text"),
    FieldDef("website", "Website", "c.website_url", "text"),
    FieldDef("description", "Description", "c.description", "text"),
    FieldDef("city", "City", "c.city", "text"),
    FieldDef("region", "Region", "c.region", "text"),
    FieldDef("country", "Country", "c.country", "text"),
    FieldDef("location", "Location", "concat_ws(', ', c.city, c.country)", "text"),
    FieldDef("industry", "Industry", "c.industry", "text"),
    FieldDef("employee_count", "Company size", "coalesce(c.employee_max, c.employee_min)", "number"),
    FieldDef("employee_min", "Employees (min)", "c.employee_min", "number"),
    FieldDef("employee_max", "Employees (max)", "c.employee_max", "number"),
    FieldDef("phone", "Phone", "c.phone", "text"),
    FieldDef("registry_id", "Registry ID", "c.registry_id", "text"),
    FieldDef("company_confidence", "Company confidence", "c.company_confidence", "percent"),
    FieldDef(
        "people_count",
        "People found",
        "(SELECT count(*) FROM people px WHERE px.company_id = c.id)",
        "number",
    ),
    FieldDef(
        "website_status",
        "Website status",
        "c.website_status",
        "enum",
        ("unknown", "ok", "unreachable", "parked", "blocked", "none"),
    ),
    FieldDef("first_seen_at", "First seen", "c.first_seen_at", "date"),
    FieldDef("updated_at", "Updated", "c.updated_at", "date"),
    FieldDef("last_crawled_at", "Last crawled", "c.last_crawled_at", "date"),
    FieldDef("times_exported", "Times exported", "c.times_exported", "number"),
    FieldDef(
        "needs_review", "Needs review", "CASE WHEN c.needs_review THEN 'true' ELSE 'false' END", "boolean"
    ),
]

ScopeKind = Literal["list", "people", "companies", "campaign", "review"]


@dataclass
class RowScope:
    kind: ScopeKind
    entity_type: EntityType
    list_id: uuid.UUID | None = None
    campaign_id: uuid.UUID | None = None


def custom_field_def(col: CustomColumn) -> FieldDef:
    entity_expr = "c.id" if col.entity_type == EntityType.company else "p.id"
    dv = (
        f"(SELECT v.display_value FROM custom_field_values v WHERE v.column_id = '{col.id}'::uuid "
        f"AND v.entity_type = '{col.entity_type.value}' AND v.entity_id = {entity_expr})"
    )
    if col.data_type == ColumnDataType.boolean:
        ftype = "boolean"
        sql = dv
    elif col.data_type == ColumnDataType.number:
        ftype = "number"
        sql = f"(CASE WHEN {dv} ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN CAST({dv} AS numeric) END)"
    elif col.data_type == ColumnDataType.date:
        ftype = "text"
        sql = dv
    else:
        ftype = "text"
        sql = dv
    return FieldDef(f"cf:{col.id}", col.name, sql, ftype, custom_column_id=col.id)  # type: ignore[arg-type]


async def load_columns(
    s: AsyncSession, workspace_id: uuid.UUID, list_id: uuid.UUID | None
) -> list[CustomColumn]:
    q = sa.select(CustomColumn).where(CustomColumn.workspace_id == workspace_id)
    q = (
        q.where(sa.or_(CustomColumn.list_id.is_(None), CustomColumn.list_id == list_id))
        if list_id
        else q.where(CustomColumn.list_id.is_(None))
    )
    return list((await s.scalars(q.order_by(CustomColumn.position, CustomColumn.created_at))).all())


def field_registry(entity_type: EntityType, columns: list[CustomColumn]) -> dict[str, FieldDef]:
    base = PERSON_FIELDS if entity_type == EntityType.person else COMPANY_FIELDS
    reg = {f.key: f for f in base}
    for col in columns:
        if entity_type == EntityType.company and col.entity_type == EntityType.person:
            continue
        reg[f"cf:{col.id}"] = custom_field_def(col)
    return reg


def _from_clause(scope: RowScope) -> tuple[str, str]:
    """Return (FROM … , scope predicate)."""
    if scope.entity_type == EntityType.person:
        base = (
            "FROM people p "
            "LEFT JOIN companies c ON c.id = p.company_id "
            "LEFT JOIN emails e ON e.id = p.primary_email_id "
            "LEFT JOIN LATERAL (SELECT qs.icp_score, qs.overall_confidence, qs.qualified FROM qualification_scores qs "
            "  WHERE qs.person_id = p.id ORDER BY qs.computed_at DESC LIMIT 1) q ON TRUE "
        )
        if scope.kind == "list":
            return (
                base + "JOIN list_memberships m ON m.person_id = p.id AND m.list_id = :scope_list ",
                "p.workspace_id = :ws",
            )
        if scope.kind == "campaign":
            return base, (
                "p.workspace_id = :ws AND EXISTS (SELECT 1 FROM lead_exposures le WHERE le.entity_type = 'person' "
                "AND le.entity_id = p.id AND le.exposure_type = 'DISCOVERED' AND le.campaign_id = :scope_campaign)"
            )
        if scope.kind == "review":
            return base, (
                "p.workspace_id = :ws AND (p.needs_review OR c.needs_review OR e.status IN ('RISKY','CATCH_ALL') "
                "OR (p.identity_confidence BETWEEN 0.5 AND 0.79)) AND (p.times_discovered > 0 OR EXISTS "
                "(SELECT 1 FROM list_memberships lm WHERE lm.person_id = p.id))"
            )
        return base, (
            "p.workspace_id = :ws AND (p.times_discovered > 0 OR EXISTS (SELECT 1 FROM list_memberships lm "
            "WHERE lm.person_id = p.id))"
        )
    base = "FROM companies c "
    if scope.kind == "list":
        return (
            base + "JOIN list_memberships m ON m.company_id = c.id AND m.list_id = :scope_list ",
            "c.workspace_id = :ws",
        )
    if scope.kind == "campaign":
        return base, (
            "c.workspace_id = :ws AND EXISTS (SELECT 1 FROM lead_exposures le WHERE le.entity_type = 'company' "
            "AND le.entity_id = c.id AND le.exposure_type = 'DISCOVERED' AND le.campaign_id = :scope_campaign)"
        )
    return base, (
        "c.workspace_id = :ws AND (c.times_discovered > 0 OR EXISTS (SELECT 1 FROM list_memberships lm "
        "WHERE lm.company_id = c.id) OR EXISTS (SELECT 1 FROM people pp JOIN list_memberships lm2 ON "
        "lm2.person_id = pp.id WHERE pp.company_id = c.id))"
    )


def _select_list(scope: RowScope) -> str:
    if scope.entity_type == EntityType.person:
        cols = [
            "p.id AS id",
            "p.company_id",
            "p.full_name",
            "p.first_name",
            "p.last_name",
            "p.job_title AS title",
            "p.normalized_title",
            "p.seniority",
            "p.role_family",
            "p.decision_power",
            "p.public_profile_url AS profile_url",
            "p.identity_confidence AS person_confidence",
            "c.name AS company",
            "c.normalized_domain AS domain",
            "c.website_url AS website",
            "c.city",
            "c.region",
            "c.country",
            "c.industry",
            "c.employee_min",
            "c.employee_max",
            "c.company_confidence",
            "coalesce(p.phone, c.phone) AS phone",
            "e.address AS email",
            "e.status AS email_status",
            "e.overall_confidence AS email_confidence",
            "e.last_checked_at AS email_checked_at",
            "q.icp_score",
            "q.overall_confidence",
            "q.qualified",
            "p.first_seen_at",
            "greatest(p.updated_at, c.updated_at) AS updated_at",
            "p.times_exported",
            "p.last_exported_at",
            "(p.needs_review OR coalesce(c.needs_review, false)) AS needs_review",
            "c.last_crawled_at",
        ]
    else:
        cols = [
            "c.id AS id",
            "c.id AS company_id",
            "c.name AS company",
            "c.normalized_domain AS domain",
            "c.website_url AS website",
            "c.description",
            "c.city",
            "c.region",
            "c.country",
            "c.industry",
            "c.employee_min",
            "c.employee_max",
            "c.phone",
            "c.registry_id",
            "c.company_confidence",
            "c.website_status",
            "c.first_seen_at",
            "c.updated_at",
            "c.last_crawled_at",
            "c.times_exported",
            "c.needs_review",
            "(SELECT count(*) FROM people px WHERE px.company_id = c.id) AS people_count",
        ]
    if scope.kind == "list":
        cols += ["m.added_at", "m.id AS membership_id"]
    return ", ".join(cols)


def encode_cursor(values: list[Any], row_id: uuid.UUID) -> str:
    def conv(v: Any) -> Any:
        if isinstance(v, datetime):
            return {"t": "dt", "v": v.isoformat()}
        if isinstance(v, Decimal):
            return {"t": "num", "v": str(v)}
        if isinstance(v, uuid.UUID):
            return str(v)
        return v

    raw = orjson.dumps({"v": [conv(v) for v in values], "id": str(row_id)})
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[list[Any], uuid.UUID]:
    try:
        pad = "=" * (-len(cursor) % 4)
        data = orjson.loads(base64.urlsafe_b64decode(cursor + pad))
        values: list[Any] = []
        for v in data["v"]:
            if isinstance(v, dict) and v.get("t") == "dt":
                values.append(datetime.fromisoformat(v["v"]))
            elif isinstance(v, dict) and v.get("t") == "num":
                values.append(Decimal(v["v"]))
            else:
                values.append(v)
        return values, uuid.UUID(data["id"])
    except Exception as exc:
        raise ValidationFailed("Invalid cursor") from exc


@dataclass
class RowsResult:
    rows: list[dict[str, Any]]
    next_cursor: str | None
    total: int
    columns: list[CustomColumn]


def _sort_sql_expr(f: FieldDef) -> str:
    if f.type in ("number", "percent"):
        return f"CAST({f.sql} AS numeric)"
    if f.type in ("text", "enum", "boolean"):
        return f"lower(CAST({f.sql} AS text))"
    return f.sql


def _cast_for(f: FieldDef) -> str:
    if f.type in ("number", "percent"):
        return "numeric"
    if f.type == "date":
        return "timestamptz"
    return "text"


async def query_rows(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    scope: RowScope,
    *,
    filters: FilterGroup | None = None,
    sort: list[SortSpec] | None = None,
    search: str | None = None,
    cursor: str | None = None,
    limit: int = 200,
    with_total: bool = True,
    ids: list[uuid.UUID] | None = None,
) -> RowsResult:
    limit = max(1, min(limit, 1000))
    columns = await load_columns(s, workspace_id, scope.list_id)
    reg = field_registry(scope.entity_type, columns)
    comp = Compiler(reg)
    from_sql, scope_pred = _from_clause(scope)
    where = [scope_pred, comp.where(filters)]
    params: dict[str, Any] = {"ws": workspace_id}
    if scope.list_id:
        params["scope_list"] = scope.list_id
    if scope.campaign_id:
        params["scope_campaign"] = scope.campaign_id
    if search and search.strip():
        needle = f"%{search.strip().lower()}%"
        params["search"] = needle
        if scope.entity_type == EntityType.person:
            where.append(
                "(lower(p.full_name) LIKE :search OR lower(coalesce(c.name,'')) LIKE :search OR "
                "lower(coalesce(e.address,'')) LIKE :search OR lower(coalesce(c.normalized_domain,'')) LIKE :search "
                "OR lower(coalesce(p.job_title,'')) LIKE :search)"
            )
        else:
            where.append(
                "(lower(c.name) LIKE :search OR lower(coalesce(c.normalized_domain,'')) LIKE :search OR "
                "lower(coalesce(c.city,'')) LIKE :search)"
            )
    id_col = "p.id" if scope.entity_type == EntityType.person else "c.id"
    if ids is not None:
        params["only_ids"] = ids
        where.append(f"{id_col} = ANY(:only_ids)")
    sort = sort or [SortSpec(field="added_at" if scope.kind == "list" else "updated_at", direction="desc")]
    # added_at: list membership time in list scope, delivery time in campaign scope (live runs append, in order)
    sort_keys: list[tuple[FieldDef, str]] = []
    for sspec in sort[:3]:
        if sspec.field == "added_at":
            if scope.kind == "list":
                sort_keys.append((FieldDef("added_at", "Added", "m.added_at", "date"), sspec.direction))
            elif scope.kind == "campaign" and scope.campaign_id:
                et, alias = ("person", "p") if scope.entity_type == EntityType.person else ("company", "c")
                expr = (
                    "(SELECT min(le2.occurred_at) FROM lead_exposures le2 WHERE le2.entity_type = "
                    f"'{et}' AND le2.entity_id = {alias}.id AND le2.exposure_type = 'DISCOVERED' "
                    "AND le2.campaign_id = :scope_campaign)"
                )
                sort_keys.append((FieldDef("added_at", "Delivered", expr, "date"), sspec.direction))
            continue
        else:
            sort_keys.extend(comp.order_keys([sspec]))
    base_where = " AND ".join(where)
    total = 0
    if with_total:
        total = int(
            (
                await s.execute(
                    sa.text(f"SELECT count(*) {from_sql} WHERE {base_where}"), {**params, **comp.params}
                )
            ).scalar()
            or 0
        )
    # keyset predicate
    page_where = base_where
    if cursor:
        values, last_id = decode_cursor(cursor)
        if len(values) != len(sort_keys):
            raise ValidationFailed("Cursor does not match sort")
        ors: list[str] = []
        for i, (f, direction) in enumerate(sort_keys):
            eqs = []
            for j in range(i):
                fj = sort_keys[j][0]
                vj = values[j]
                if vj is None:
                    eqs.append(f"({_sort_sql_expr(fj)} IS NULL)")
                else:
                    eqs.append(f"({_sort_sql_expr(fj)} = CAST({comp.bind(vj)} AS {_cast_for(fj)}))")
            vi = values[i]
            if vi is None:
                continue  # nothing sorts after NULL at this key (NULLS LAST) except via later keys
            op = ">" if direction == "asc" else "<"
            after = f"({_sort_sql_expr(f)} {op} CAST({comp.bind(vi)} AS {_cast_for(f)}) OR {_sort_sql_expr(f)} IS NULL)"
            ors.append("(" + " AND ".join([*eqs, after]) + ")")
        eq_all = [
            f"({_sort_sql_expr(f)} IS NULL)"
            if v is None
            else f"({_sort_sql_expr(f)} = CAST({comp.bind(v)} AS {_cast_for(f)}))"
            for (f, _), v in zip(sort_keys, values, strict=True)
        ]
        ors.append("(" + " AND ".join([*eq_all, f"{id_col} > {comp.bind(last_id)}"]) + ")")
        page_where = f"{base_where} AND ({' OR '.join(ors)})"
    order_sql = ", ".join(
        [f"{_sort_sql_expr(f)} {d.upper()} NULLS LAST" for f, d in sort_keys] + [f"{id_col} ASC"]
    )
    sort_select = ", ".join(f"{_sort_sql_expr(f)} AS __s{i}" for i, (f, _) in enumerate(sort_keys))
    sql = (
        f"SELECT {_select_list(scope)}{', ' + sort_select if sort_select else ''} {from_sql} "
        f"WHERE {page_where} ORDER BY {order_sql} LIMIT :lim"
    )
    result = await s.execute(sa.text(sql), {**params, **comp.params, "lim": limit + 1})
    raw_rows = [dict(r._mapping) for r in result]
    has_more = len(raw_rows) > limit
    raw_rows = raw_rows[:limit]
    next_cursor = None
    if has_more and raw_rows:
        last = raw_rows[-1]
        next_cursor = encode_cursor([last.get(f"__s{i}") for i in range(len(sort_keys))], last["id"])
    for r in raw_rows:
        for i in range(len(sort_keys)):
            r.pop(f"__s{i}", None)
    await _attach_cells(s, raw_rows, columns, scope.entity_type)
    await _attach_sources(s, raw_rows, scope.entity_type)
    return RowsResult(rows=raw_rows, next_cursor=next_cursor, total=total, columns=columns)


async def _attach_cells(
    s: AsyncSession, rows: list[dict[str, Any]], columns: list[CustomColumn], entity_type: EntityType
) -> None:
    for r in rows:
        r["cells"] = {}
    if not rows or not columns:
        return
    person_ids = [r["id"] for r in rows] if entity_type == EntityType.person else []
    company_ids = [r["company_id"] for r in rows if r.get("company_id")]
    col_ids = [c.id for c in columns]
    q = sa.select(CustomFieldValue).where(
        CustomFieldValue.column_id.in_(col_ids),
        sa.or_(
            sa.and_(
                CustomFieldValue.entity_type == EntityType.person,
                CustomFieldValue.entity_id.in_(person_ids or [uuid.uuid4()]),
            ),
            sa.and_(
                CustomFieldValue.entity_type == EntityType.company,
                CustomFieldValue.entity_id.in_(company_ids or [uuid.uuid4()]),
            ),
        ),
    )
    by_key: dict[tuple[uuid.UUID, str, uuid.UUID], CustomFieldValue] = {}
    for v in (await s.scalars(q)).all():
        by_key[(v.column_id, v.entity_type.value, v.entity_id)] = v
    for r in rows:
        for col in columns:
            ent_id = r["company_id"] if col.entity_type == EntityType.company else r["id"]
            if ent_id is None:
                continue
            cell = by_key.get((col.id, col.entity_type.value, ent_id))
            if cell is None:
                continue
            r["cells"][str(col.id)] = {
                "v": cell.value_json,
                "d": cell.display_value,
                "s": cell.status.value,
                "c": cell.confidence,
                "u": cell.is_user_override,
                "e": cell.error,
            }


async def _attach_sources(s: AsyncSession, rows: list[dict[str, Any]], entity_type: EntityType) -> None:
    """Distinct evidence source types per row (people + their company), for the Sources column."""
    for r in rows:
        r["sources"] = []
    if not rows:
        return
    company_ids = list({r["company_id"] for r in rows if r.get("company_id")})
    srcs: dict[uuid.UUID, set[str]] = {}
    if company_ids:
        res = await s.execute(
            sa.text(
                "SELECT DISTINCT company_id, coalesce(source_key, source_type) FROM company_field_observations "
                "WHERE company_id = ANY(:ids)"
            ),
            {"ids": company_ids},
        )
        for cid, key in res:
            srcs.setdefault(cid, set()).add(key)
    if entity_type == EntityType.person:
        person_ids = [r["id"] for r in rows]
        res = await s.execute(
            sa.text(
                "SELECT DISTINCT person_id, coalesce(source_key, source_type) FROM person_field_observations "
                "WHERE person_id = ANY(:ids)"
            ),
            {"ids": person_ids},
        )
        psrcs: dict[uuid.UUID, set[str]] = {}
        for pid, key in res:
            psrcs.setdefault(pid, set()).add(key)
        for r in rows:
            cid = r.get("company_id")
            r["sources"] = sorted(psrcs.get(r["id"], set()) | (srcs.get(cid, set()) if cid else set()))
    else:
        for r in rows:
            r["sources"] = sorted(srcs.get(r["id"], set()))


async def count_rows(
    s: AsyncSession, workspace_id: uuid.UUID, scope: RowScope, *, filters: FilterGroup | None = None
) -> int:
    res = await query_rows(s, workspace_id, scope, filters=filters, limit=1, with_total=True)
    return res.total


async def select_ids(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    scope: RowScope,
    *,
    filters: FilterGroup | None = None,
    search: str | None = None,
    max_rows: int = 100_000,
) -> list[uuid.UUID]:
    """All entity ids matching a scope + filters (for bulk actions by filter)."""
    columns = await load_columns(s, workspace_id, scope.list_id)
    comp = Compiler(field_registry(scope.entity_type, columns))
    from_sql, scope_pred = _from_clause(scope)
    params: dict[str, Any] = {"ws": workspace_id, "lim": max_rows}
    if scope.list_id:
        params["scope_list"] = scope.list_id
    if scope.campaign_id:
        params["scope_campaign"] = scope.campaign_id
    where = [scope_pred, comp.where(filters)]
    if search:
        params["search"] = f"%{search.strip().lower()}%"
        where.append(
            "(lower(p.full_name) LIKE :search OR lower(coalesce(c.name,'')) LIKE :search)"
            if scope.entity_type == EntityType.person
            else "(lower(c.name) LIKE :search)"
        )
    id_col = "p.id" if scope.entity_type == EntityType.person else "c.id"
    res = await s.execute(
        sa.text(f"SELECT {id_col} {from_sql} WHERE {' AND '.join(where)} LIMIT :lim"),
        {**params, **comp.params},
    )
    return [r[0] for r in res]
