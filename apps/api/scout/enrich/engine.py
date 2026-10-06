"""Dynamic enrichment engine (PIPELINE §6.2): column definitions, fan-out into `enrichment.batch` jobs,
batch execution with input hashing, user overrides and freshness.

Cells move `queued → running → success | unknown | failed`. UNKNOWN (insufficient evidence) is distinct
from a false value and from FAILED (technical error with a human-readable reason). User overrides are
never overwritten automatically. Unchanged inputs + unchanged plan ⇒ no recomputation (no AI call).
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import orjson
import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.engine import session_scope
from scout.db.enums import (
    CellStatus,
    ColumnDataType,
    ColumnKind,
    EntityType,
    ExposureType,
    PageType,
    ResolverType,
    WebsiteStatus,
)
from scout.db.ids import uuid7
from scout.db.models import (
    Company,
    CustomColumn,
    CustomFieldValue,
    LeadExposure,
    List,
    ListMembership,
    Person,
    WebsitePage,
)
from scout.enrich.planner import WEBSITE_STRATEGIES, plan_column
from scout.enrich.resolvers import resolve
from scout.enrich.resolvers.base import NO_AI, ResolveContext, failed, unknown
from scout.enrich.types import CellResult, EnrichmentPlan
from scout.enrich.values import coerce_user_value, display_for
from scout.errors import (
    AIUnavailable,
    BlockedError,
    BudgetExceeded,
    Conflict,
    FetchError,
    JobError,
    NotFound,
    RateLimitedError,
    RetryableError,
)
from scout.learning import feedback as learning_feedback
from scout.learning.enrich import EnrichLearning
from scout.util.text import sha256_hex, slugify

log = structlog.get_logger("enrich.engine")

JOB_TYPE = "enrichment.batch"
BATCH_SIZE = 50
ENTITY_CONCURRENCY = 6
EVENT_FLUSH_EVERY = 25
ORPHAN_AFTER = timedelta(minutes=30)  # queued/running cells older than this are re-queued
_IN_CHUNK = 5000
# Strategies whose result is a pure function of (plan, pages, extra inputs): safe to skip when unchanged.
SKIPPABLE = frozenset(WEBSITE_STRATEGIES | {"web_research"})
AI_STRATEGIES = frozenset({"semantic_classifier", "ai_extraction", "generated_text", "web_research"})
STRICT_WEBSITE = frozenset(WEBSITE_STRATEGIES - {"generated_text"})


def _chunks[T](items: Sequence[T], n: int) -> Iterable[Sequence[T]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def plan_of(column: CustomColumn) -> EnrichmentPlan:
    return EnrichmentPlan.model_validate(column.configuration)


def _refresh_days(column: CustomColumn, plan: EnrichmentPlan | None = None) -> int:
    days = (column.refresh_policy or {}).get("refresh_days")
    try:
        return max(1, int(days)) if days is not None else (plan.refresh_days if plan else 30)
    except (TypeError, ValueError):
        return plan.refresh_days if plan else 30


async def _get_column(s: AsyncSession, workspace_id: uuid.UUID, column_id: uuid.UUID) -> CustomColumn:
    col = await s.scalar(
        sa.select(CustomColumn).where(CustomColumn.id == column_id, CustomColumn.workspace_id == workspace_id)
    )
    if col is None:
        raise NotFound("Column not found", code="column_not_found")
    return col


# =============================================================================================
# Pages (cross-module: scout.crawl.cache, imported lazily)
# =============================================================================================
async def cached_pages(workspace_id: uuid.UUID, company_id: uuid.UUID) -> list[WebsitePage]:
    """Cached pages for a company (scout.crawl.cache when available, else a direct query)."""
    try:
        from scout.crawl.cache import get_cached_pages
    except ImportError:
        get_cached_pages = None  # type: ignore[assignment]
    if get_cached_pages is not None:
        res: Any = get_cached_pages(workspace_id, company_id)
        if inspect.isawaitable(res):
            res = await res
        return list(res or [])
    async with session_scope() as s:
        rows = await s.scalars(
            sa.select(WebsitePage)
            .where(WebsitePage.workspace_id == workspace_id, WebsitePage.company_id == company_id)
            .order_by(WebsitePage.fetched_at)
        )
        return list(rows.all())


def _has_website(company: Company) -> bool:
    return bool(
        company.domain or company.normalized_domain or company.website_url
    ) and company.website_status not in (WebsiteStatus.none,)


async def _no_pages_reason(plan: EnrichmentPlan, company: Company) -> CellResult:
    async with session_scope() as s:
        status = await s.scalar(sa.select(Company.website_status).where(Company.id == company.id))
    resolver = plan.strategy
    if not _has_website(company) or status == WebsiteStatus.none:
        return unknown(plan, resolver=resolver, error="No website")
    if status == WebsiteStatus.unreachable:
        return failed(plan, resolver=resolver, error="Website unreachable")
    if status == WebsiteStatus.blocked:
        return failed(plan, resolver=resolver, error="Website blocked our crawler")
    if status == WebsiteStatus.parked:
        return unknown(plan, resolver=resolver, error="Parked domain — no content")
    return unknown(plan, resolver=resolver, error="Website not crawled")


# =============================================================================================
# Hashing & error mapping
# =============================================================================================
def compute_input_hash(plan: EnrichmentPlan, pages: Sequence[Any], extra: str = "") -> str:
    """sha256(plan JSON + sorted page content hashes + strategy-specific extra inputs)."""
    plan_json = orjson.dumps(plan.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS).decode()
    page_part = "|".join(sorted(str(getattr(p, "content_hash", "") or "") for p in pages))
    return sha256_hex(f"{plan_json}\n{page_part}\n{extra}")


def _extra_inputs(
    plan: EnrichmentPlan,
    company: Company | None,
    person: Person | None,
    factual: dict[str, str],
    deps: dict[str, Any],
) -> str:
    parts: list[Any] = []
    if plan.strategy in AI_STRATEGIES:
        from scout.ai.factory import get_ai

        ai = get_ai()
        parts.append(f"ai={ai.name if ai.available else 'none'}")  # results change when AI becomes available
    if plan.strategy in {"website_field", "social_profile"} and company is not None:
        parts += [company.phone, company.address, company.postal_code, company.city, company.linkedin_url]
    if plan.strategy in {"generated_text", "web_research"}:
        if company is not None:
            parts += [company.name, company.domain, company.city, company.description]
        if person is not None:
            parts += [person.full_name, person.job_title]
    if plan.strategy == "generated_text":
        parts.append(sorted(factual.items()))
    if deps:
        parts.append(sorted((k, str(v)) for k, v in deps.items()))
    return orjson.dumps(parts, default=str).decode()


def _human_error(exc: BaseException) -> str:
    if isinstance(exc, BlockedError):
        return "Source blocked our requests"
    if isinstance(exc, RateLimitedError):
        return "Rate-limited — retry later"
    if isinstance(exc, FetchError):
        return "Website unreachable"
    if isinstance(exc, TimeoutError):
        return "Timed out"
    if isinstance(exc, RetryableError):
        msg = str(exc)
        return (
            "AI request failed — retry later"
            if "gemini" in msg.lower() or "ai" in msg.lower()
            else "Temporary error — retry later"
        )
    if isinstance(exc, JobError):
        return str(exc)[:200] or "Enrichment error"
    return "Enrichment error (internal)"


async def _safe_resolve(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    try:
        return await resolve(rc)
    except AIUnavailable:
        return unknown(plan, resolver=plan.strategy, error=NO_AI)
    except BudgetExceeded:
        return unknown(plan, resolver=plan.strategy, error="Budget limit reached")
    except Exception as exc:
        if not isinstance(exc, JobError | TimeoutError):
            log.exception("enrich.resolver_crashed", strategy=plan.strategy)
        return failed(plan, resolver=plan.strategy, error=_human_error(exc))


async def resolve_plan(
    workspace_id: uuid.UUID,
    plan: EnrichmentPlan,
    *,
    company: Company | None,
    person: Person | None = None,
    pages: list[WebsitePage] | None = None,
) -> CellResult:
    """Run a plan for one entity without persisting anything (column previews, campaign stages)."""
    if (
        pages is None
        and company is not None
        and (plan.strategy in WEBSITE_STRATEGIES or plan.strategy == "tech_detection")
    ):
        pages = await cached_pages(workspace_id, company.id)
    rc = ResolveContext(
        plan=plan, workspace_id=workspace_id, company=company, person=person, pages=list(pages or [])
    )
    return await _safe_resolve(rc)


# =============================================================================================
# Column definitions
# =============================================================================================
async def create_column(
    workspace_id: uuid.UUID,
    *,
    name: str,
    instruction: str | None,
    list_id: uuid.UUID | None = None,
    data_type: ColumnDataType | None = None,
    plan: EnrichmentPlan | None = None,
    created_by: str | None = None,
) -> CustomColumn:
    """Plan (unless given) and persist a custom column with a unique slug per (workspace, list)."""
    name = (name or "").strip() or "Column"
    if list_id is not None:
        async with session_scope() as s:
            owner = await s.scalar(sa.select(List.workspace_id).where(List.id == list_id))
        if owner != workspace_id:
            raise NotFound("List not found")
    plan = plan or await plan_column(name, instruction, data_type=data_type)
    base = slugify(name)
    scope = [CustomColumn.workspace_id == workspace_id, CustomColumn.list_id.is_not_distinct_from(list_id)]
    for attempt in range(5):
        try:
            async with session_scope() as s:
                taken = set(
                    (
                        await s.scalars(
                            sa.select(CustomColumn.slug).where(
                                *scope, CustomColumn.slug.startswith(base, autoescape=True)
                            )
                        )
                    ).all()
                )
                slug, n = base, 2
                while slug in taken:
                    slug, n = f"{base}_{n}", n + 1
                position = await s.scalar(
                    sa.select(sa.func.coalesce(sa.func.max(CustomColumn.position), -1)).where(*scope)
                )
                col = CustomColumn(
                    id=uuid7(),
                    workspace_id=workspace_id,
                    list_id=list_id,
                    name=name,
                    slug=slug,
                    data_type=plan.data_type,
                    kind=plan.kind,
                    entity_type=plan.entity_type,
                    resolver_type=plan.resolver,
                    instructions=instruction or "",
                    configuration=plan.model_dump(mode="json"),
                    source_preferences=[str(getattr(x, "value", x)) for x in plan.input_sources],
                    confidence_threshold=plan.confidence_threshold,
                    refresh_policy={"refresh_days": plan.refresh_days},
                    depends_on=[],
                    position=int(position or 0) + 1,
                    created_by=created_by,
                )
                s.add(col)
                await s.flush()
            log.info("enrich.column_created", column_id=str(col.id), strategy=plan.strategy, slug=slug)
            return col
        except IntegrityError:
            if attempt == 4:
                raise Conflict(
                    "Could not allocate a unique column slug", code="column_slug_conflict"
                ) from None
    raise AssertionError("unreachable")


async def _mark_cells_stale(
    s: AsyncSession, column_id: uuid.UUID, entity_ids: Sequence[uuid.UUID] | None = None
) -> int:
    stmt = (
        sa.update(CustomFieldValue)
        .where(
            CustomFieldValue.column_id == column_id,
            CustomFieldValue.is_user_override.is_(False),
            CustomFieldValue.status.in_([CellStatus.success, CellStatus.unknown, CellStatus.failed]),
        )
        .values(status=CellStatus.stale, updated_at=sa.func.now())
    )
    if entity_ids is not None:
        stmt = stmt.where(CustomFieldValue.entity_id.in_(list(entity_ids)))
    res = await s.execute(stmt)
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


async def update_column_definition(
    workspace_id: uuid.UUID,
    column_id: uuid.UUID,
    *,
    instruction: str | None = None,
    data_type: ColumnDataType | None = None,
    confidence_threshold: float | None = None,
    refresh_days: int | None = None,
    source_preferences: list[str] | None = None,
) -> CustomColumn:
    """Advanced edit. A new instruction re-plans the column; result-affecting changes mark cells stale."""
    async with session_scope() as s:
        col = await _get_column(s, workspace_id, column_id)
    plan = plan_of(col)
    stale = False
    new_instruction = None
    if instruction is not None and instruction.strip() != (col.instructions or "").strip():
        plan = await plan_column(col.name, instruction, data_type=data_type)
        new_instruction, stale = instruction.strip(), True
    elif data_type is not None and data_type != plan.data_type:
        plan = plan.model_copy(update={"data_type": data_type})
        stale = True
    if confidence_threshold is not None and abs(confidence_threshold - plan.confidence_threshold) > 1e-9:
        plan = plan.model_copy(update={"confidence_threshold": max(0.0, min(1.0, confidence_threshold))})
        stale = True
    if refresh_days is not None:
        plan = plan.model_copy(update={"refresh_days": max(1, int(refresh_days))})
    if source_preferences is not None:
        valid = {p.value for p in PageType}
        types = [PageType(x) for x in source_preferences if x in valid]
        if types != list(plan.input_sources):
            plan = plan.model_copy(update={"input_sources": types})
            stale = True
    plan = EnrichmentPlan.model_validate(plan.model_dump(mode="json"))
    async with session_scope() as s:
        col = await _get_column(s, workspace_id, column_id)
        old_entity = col.entity_type
        if new_instruction is not None:
            col.instructions = new_instruction
        if source_preferences is not None:
            col.source_preferences = list(source_preferences)
        col.configuration = plan.model_dump(mode="json")
        col.data_type, col.kind, col.entity_type = plan.data_type, plan.kind, plan.entity_type
        col.resolver_type = plan.resolver
        col.confidence_threshold = plan.confidence_threshold
        col.refresh_policy = {**(col.refresh_policy or {}), "refresh_days": plan.refresh_days}
        if old_entity != plan.entity_type:
            await s.execute(
                sa.delete(CustomFieldValue).where(
                    CustomFieldValue.column_id == column_id,
                    CustomFieldValue.entity_type == old_entity,
                    CustomFieldValue.is_user_override.is_(False),
                )
            )
        if stale:
            await _mark_cells_stale(s, column_id)
    return col


# =============================================================================================
# Target entities & coverage
# =============================================================================================
async def column_entity_ids(
    workspace_id: uuid.UUID,
    column: CustomColumn,
    *,
    list_id: uuid.UUID | None = None,
    person_ids: Sequence[uuid.UUID] | None = None,
    company_ids: Sequence[uuid.UUID] | None = None,
) -> list[uuid.UUID]:
    """Entities a column applies to: company-level → distinct companies (of the people) in the list or
    selection; person-level → people. Without list or selection: every entity of the workspace."""
    list_id = list_id if list_id is not None else column.list_id
    lm = ListMembership
    q: Any
    if column.entity_type == EntityType.company:
        if company_ids is not None:
            q = sa.select(Company.id).where(
                Company.workspace_id == workspace_id, Company.id.in_(list(company_ids))
            )
        elif person_ids is not None:
            q = sa.select(Person.company_id).where(
                Person.workspace_id == workspace_id,
                Person.id.in_(list(person_ids)),
                Person.company_id.is_not(None),
            )
        elif list_id is not None:
            q = (
                sa.select(sa.func.coalesce(lm.company_id, Person.company_id))
                .select_from(lm)
                .outerjoin(Person, Person.id == lm.person_id)
                .where(lm.list_id == list_id, lm.workspace_id == workspace_id)
            )
        else:
            q = sa.select(Company.id).where(Company.workspace_id == workspace_id)
    else:
        if person_ids is not None:
            q = sa.select(Person.id).where(
                Person.workspace_id == workspace_id, Person.id.in_(list(person_ids))
            )
        elif company_ids is not None:
            q = sa.select(Person.id).where(
                Person.workspace_id == workspace_id, Person.company_id.in_(list(company_ids))
            )
        elif list_id is not None:
            direct = sa.select(lm.person_id).where(
                lm.list_id == list_id, lm.workspace_id == workspace_id, lm.person_id.is_not(None)
            )
            via_company = sa.select(Person.id).where(
                Person.workspace_id == workspace_id,
                Person.company_id.in_(
                    sa.select(lm.company_id).where(lm.list_id == list_id, lm.company_id.is_not(None))
                ),
            )
            q = sa.union(direct, via_company)
        else:
            q = sa.select(Person.id).where(Person.workspace_id == workspace_id)
    async with session_scope() as s:
        ids = {i for i in (await s.execute(q)).scalars().all() if i is not None}
    return sorted(ids)


async def estimate_coverage(
    workspace_id: uuid.UUID, column: CustomColumn, entity_ids: Sequence[uuid.UUID]
) -> dict[str, int]:
    """{"total", "cached", "done"}: cached = entities whose company already has cached pages
    (UI: “2,942 / 3,000 already cached”); done = cells already resolved (success/unknown)."""
    ids = list(entity_ids)
    cached = done = 0
    async with session_scope() as s:
        for chunk in _chunks(ids, _IN_CHUNK):
            if column.entity_type == EntityType.company:
                has_pages = sa.exists().where(WebsitePage.company_id == Company.id)
                q = (
                    sa.select(sa.func.count())
                    .select_from(Company)
                    .where(Company.workspace_id == workspace_id, Company.id.in_(list(chunk)), has_pages)
                )
            else:
                has_pages = sa.exists().where(WebsitePage.company_id == Person.company_id)
                q = (
                    sa.select(sa.func.count())
                    .select_from(Person)
                    .where(Person.workspace_id == workspace_id, Person.id.in_(list(chunk)), has_pages)
                )
            cached += int(await s.scalar(q) or 0)
            done += int(
                await s.scalar(
                    sa.select(sa.func.count())
                    .select_from(CustomFieldValue)
                    .where(
                        CustomFieldValue.column_id == column.id,
                        CustomFieldValue.entity_id.in_(list(chunk)),
                        CustomFieldValue.status.in_([CellStatus.success, CellStatus.unknown]),
                    )
                )
                or 0
            )
    return {"total": len(ids), "cached": cached, "done": done}


# =============================================================================================
# Progress & events
# =============================================================================================
async def column_progress(workspace_id: uuid.UUID, column_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                sa.select(CustomFieldValue.status, sa.func.count())
                .where(CustomFieldValue.column_id == column_id, CustomFieldValue.workspace_id == workspace_id)
                .group_by(CustomFieldValue.status)
            )
        ).all()
    counts = {str(getattr(st, "value", st)): int(n) for st, n in rows}
    return {
        "column_id": str(column_id),
        "done": counts.get("success", 0) + counts.get("unknown", 0) + counts.get("failed", 0),
        "total": sum(counts.values()),
        "unknown": counts.get("unknown", 0),
        "failed": counts.get("failed", 0),
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "stale": counts.get("stale", 0),
    }


async def _emit_progress(
    workspace_id: uuid.UUID,
    column_id: uuid.UUID,
    *,
    campaign_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> None:
    from scout.jobs.events import emit

    await emit(
        workspace_id,
        "column.progress",
        await column_progress(workspace_id, column_id),
        campaign_id=campaign_id,
        job_id=job_id,
    )


# =============================================================================================
# Fan-out
# =============================================================================================
async def _upsert_status(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    column_id: uuid.UUID,
    entity_type: EntityType,
    entity_ids: Sequence[uuid.UUID],
    status: CellStatus,
) -> None:
    for chunk in _chunks(list(entity_ids), 1000):
        stmt = pg_insert(CustomFieldValue).values(
            [
                {
                    "id": uuid7(),
                    "workspace_id": workspace_id,
                    "column_id": column_id,
                    "entity_type": entity_type,
                    "entity_id": eid,
                    "status": status,
                }
                for eid in chunk
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["column_id", "entity_type", "entity_id"],
            set_={"status": status, "error": None, "updated_at": sa.func.now()},
            where=CustomFieldValue.is_user_override.is_(False),
        )
        await s.execute(stmt)


async def enqueue_column(
    workspace_id: uuid.UUID,
    column_id: uuid.UUID,
    *,
    entity_ids: Sequence[uuid.UUID] | None = None,
    list_id: uuid.UUID | None = None,
    only_missing: bool = True,
    force: bool = False,
    campaign_id: uuid.UUID | None = None,
    refresh: bool = False,
    max_page_age_days: int | None = None,
) -> int:
    """Queue cells and fan out `enrichment.batch` jobs (≤ 50 entities each). Returns the number of cells scheduled.

    User overrides are never queued. With `only_missing`, fresh success/unknown cells are skipped; without it
    they are scheduled but keep their value (the batch recomputes only if inputs changed). `force` recomputes all.
    """
    from scout.jobs.queue import enqueue

    async with session_scope() as s:
        col = await _get_column(s, workspace_id, column_id)
    ids = (
        list(entity_ids)
        if entity_ids is not None
        else await column_entity_ids(workspace_id, col, list_id=list_id)
    )
    ids = list(dict.fromkeys(ids))
    if not ids:
        await _emit_progress(workspace_id, column_id, campaign_id=campaign_id)
        return 0
    plan = plan_of(col)
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=_refresh_days(col, plan))
    etype = EntityType(col.entity_type)
    to_queue: list[uuid.UUID] = []
    to_check: list[uuid.UUID] = []
    async with session_scope() as s:
        existing: dict[uuid.UUID, Any] = {}
        for chunk in _chunks(ids, _IN_CHUNK):
            rows = (
                await s.execute(
                    sa.select(
                        CustomFieldValue.entity_id,
                        CustomFieldValue.status,
                        CustomFieldValue.is_user_override,
                        CustomFieldValue.observed_at,
                        CustomFieldValue.updated_at,
                    ).where(
                        CustomFieldValue.column_id == column_id,
                        CustomFieldValue.entity_type == etype,
                        CustomFieldValue.entity_id.in_(list(chunk)),
                    )
                )
            ).all()
            existing.update({r.entity_id: r for r in rows})
        for eid in ids:
            cell = existing.get(eid)
            if cell is None:
                to_queue.append(eid)
                continue
            if cell.is_user_override:
                continue
            if force:
                to_queue.append(eid)
            elif (
                cell.status in (CellStatus.success, CellStatus.unknown)
                and cell.observed_at
                and cell.observed_at >= cutoff
            ):
                if not only_missing or refresh:
                    to_check.append(eid)
            elif (
                cell.status in (CellStatus.queued, CellStatus.running)
                and cell.updated_at
                and now - cell.updated_at < ORPHAN_AFTER
            ):
                continue  # already in flight
            else:
                to_queue.append(eid)
        if to_queue:
            await _upsert_status(s, workspace_id, column_id, etype, to_queue, CellStatus.queued)
        targets = to_queue + to_check
        for chunk in _chunks(targets, BATCH_SIZE):
            keys = ",".join(sorted(str(x) for x in chunk))
            payload: dict[str, Any] = {
                "column_id": str(column_id),
                "entity_ids": [str(x) for x in chunk],
                "force": force,
                "refresh": refresh,
            }
            if max_page_age_days is not None:
                payload["max_page_age_days"] = int(max_page_age_days)
            mode = "f" if force else "r" if refresh else "n"
            await enqueue(
                s,
                workspace_id=workspace_id,
                type=JOB_TYPE,
                payload=payload,
                campaign_id=campaign_id,
                dedupe_key=f"enrich:{column_id}:{mode}:{sha256_hex(keys)[:24]}",
            )
    log.info("enrich.enqueued", column_id=str(column_id), queued=len(to_queue), rechecked=len(to_check))
    await _emit_progress(workspace_id, column_id, campaign_id=campaign_id)
    return len(targets)


# =============================================================================================
# Batch execution
# =============================================================================================
class _Batch:
    """Executes one `enrichment.batch`: bounded concurrency, shared page loads per company, batched events."""

    def __init__(
        self,
        workspace_id: uuid.UUID,
        column: CustomColumn,
        *,
        force: bool,
        refresh: bool,
        max_page_age_days: int | None,
        campaign_id: uuid.UUID | None,
        job_id: uuid.UUID | None,
    ) -> None:
        self.ws = workspace_id
        self.column = column
        self.plan = plan_of(column)
        self.etype = EntityType(column.entity_type)
        self.force, self.refresh = force, refresh
        self.max_page_age_days = max_page_age_days
        self.campaign_id, self.job_id = campaign_id, job_id
        self.cutoff = datetime.now(UTC) - timedelta(days=_refresh_days(column, self.plan))
        self.sem = asyncio.Semaphore(ENTITY_CONCURRENCY)
        self.cells: dict[uuid.UUID, CustomFieldValue] = {}
        self.companies: dict[uuid.UUID, Company] = {}
        self.people: dict[uuid.UUID, Person] = {}
        self.factual: dict[uuid.UUID, dict[str, str]] = {}
        self.deps: dict[uuid.UUID, dict[str, Any]] = {}
        self._pages: dict[uuid.UUID, asyncio.Future[tuple[list[WebsitePage], CellResult | None]]] = {}
        self._events: list[dict[str, Any]] = []
        self._event_lock = asyncio.Lock()
        self._exposed_companies: set[uuid.UUID] = set()
        self.stats = {"processed": 0, "unchanged": 0, "skipped": 0, "success": 0, "unknown": 0, "failed": 0}
        self.learning = EnrichLearning(use=False)  # Empirical Source Scoring (attempts + learned confidence)

    # ------------------------------------------------------------------ loading
    async def load(self, entity_ids: Sequence[uuid.UUID]) -> None:
        ids = list(entity_ids)
        async with session_scope() as s:
            self.cells = {
                c.entity_id: c
                for c in (
                    await s.scalars(
                        sa.select(CustomFieldValue).where(
                            CustomFieldValue.column_id == self.column.id,
                            CustomFieldValue.entity_type == self.etype,
                            CustomFieldValue.entity_id.in_(ids),
                        )
                    )
                ).all()
            }
            if self.etype == EntityType.company:
                company_ids = ids
            else:
                self.people = {
                    p.id: p
                    for p in (
                        await s.scalars(
                            sa.select(Person).where(Person.workspace_id == self.ws, Person.id.in_(ids))
                        )
                    ).all()
                }
                company_ids = list({p.company_id for p in self.people.values() if p.company_id})
            if company_ids:
                self.companies = {
                    c.id: c
                    for c in (
                        await s.scalars(
                            sa.select(Company).where(
                                Company.workspace_id == self.ws, Company.id.in_(company_ids)
                            )
                        )
                    ).all()
                }
            if self.plan.strategy == "generated_text":
                await self._load_factual(s, ids)
            if self.plan.depends_on:
                await self._load_dependencies(s, ids)

    def _entity_keys(self, eid: uuid.UUID) -> list[tuple[EntityType, uuid.UUID]]:
        keys = [(self.etype, eid)]
        if self.etype == EntityType.person:
            p = self.people.get(eid)
            if p is not None and p.company_id:
                keys.append((EntityType.company, p.company_id))
        return keys

    async def _cell_values(
        self, s: AsyncSession, columns: dict[uuid.UUID, CustomColumn], ids: Sequence[uuid.UUID]
    ) -> dict[tuple[EntityType, uuid.UUID], dict[uuid.UUID, Any]]:
        keys = {k for eid in ids for k in self._entity_keys(eid)}
        out: dict[tuple[EntityType, uuid.UUID], dict[uuid.UUID, Any]] = {}
        if not columns or not keys:
            return out
        rows = (
            await s.execute(
                sa.select(
                    CustomFieldValue.column_id,
                    CustomFieldValue.entity_type,
                    CustomFieldValue.entity_id,
                    CustomFieldValue.value_json,
                    CustomFieldValue.display_value,
                ).where(
                    CustomFieldValue.column_id.in_(list(columns)),
                    CustomFieldValue.status == CellStatus.success,
                    CustomFieldValue.entity_id.in_([k[1] for k in keys]),
                )
            )
        ).all()
        for r in rows:
            out.setdefault((EntityType(r.entity_type), r.entity_id), {})[r.column_id] = (
                r.value_json,
                r.display_value,
            )
        return out

    async def _load_factual(self, s: AsyncSession, ids: Sequence[uuid.UUID]) -> None:
        """Existing *factual* column values (never generated ones) as extra input for generated text."""
        cols = {
            c.id: c
            for c in (
                await s.scalars(
                    sa.select(CustomColumn)
                    .where(
                        CustomColumn.workspace_id == self.ws,
                        CustomColumn.id != self.column.id,
                        CustomColumn.kind == ColumnKind.factual,
                        sa.or_(
                            CustomColumn.list_id.is_(None),
                            CustomColumn.list_id.is_not_distinct_from(self.column.list_id),
                        ),
                    )
                    .order_by(CustomColumn.position)
                    .limit(30)
                )
            ).all()
        }
        values = await self._cell_values(s, cols, ids)
        for eid in ids:
            facts: dict[str, str] = {}
            for key in self._entity_keys(eid):
                for cid, (_v, display) in values.get(key, {}).items():
                    if display:
                        facts[cols[cid].name] = str(display)[:200]
            self.factual[eid] = facts

    async def _load_dependencies(self, s: AsyncSession, ids: Sequence[uuid.UUID]) -> None:
        refs = list(self.plan.depends_on)
        uuids = []
        for r in refs:
            try:
                uuids.append(uuid.UUID(r))
            except ValueError:
                continue
        cols = {
            c.id: c
            for c in (
                await s.scalars(
                    sa.select(CustomColumn).where(
                        CustomColumn.workspace_id == self.ws,
                        sa.or_(CustomColumn.id.in_(uuids), CustomColumn.slug.in_(refs)),
                    )
                )
            ).all()
        }
        values = await self._cell_values(s, cols, ids)
        for eid in ids:
            found: dict[str, Any] = {}
            for key in self._entity_keys(eid):
                for cid, (value, _d) in values.get(key, {}).items():
                    col = cols[cid]
                    for ref in (str(col.id), col.slug):
                        if ref in refs:
                            found.setdefault(ref, value)
            self.deps[eid] = found

    # ------------------------------------------------------------------ pages
    async def pages_for(
        self, company: Company, *, refresh: bool
    ) -> tuple[list[WebsitePage], CellResult | None]:
        fut = self._pages.get(company.id)
        if fut is None:
            fut = asyncio.ensure_future(self._load_pages(company, refresh=refresh))
            self._pages[company.id] = fut
        return await fut

    async def _load_pages(
        self, company: Company, *, refresh: bool
    ) -> tuple[list[WebsitePage], CellResult | None]:
        plan = self.plan
        pages = await cached_pages(self.ws, company.id)
        if plan.strategy not in WEBSITE_STRATEGIES:
            return pages, None
        recrawl = plan.resolver == ResolverType.WEBSITE_RECRAWL
        if (not pages or refresh or recrawl) and _has_website(company):
            try:
                from scout.crawl.cache import ensure_crawled
            except ImportError:
                ensure_crawled = None  # type: ignore[assignment]
            if ensure_crawled is not None:
                max_age = self.max_page_age_days or (plan.refresh_days if (refresh or recrawl) else 30)
                try:
                    crawled = await ensure_crawled(
                        self.ws, company.id, max_age_days=max_age, force=bool(self.force and recrawl)
                    )
                    pages = list(crawled or []) or pages
                except BlockedError:
                    if not pages:
                        return [], failed(plan, resolver=plan.strategy, error="Website blocked our crawler")
                except (FetchError, RetryableError, TimeoutError):
                    if not pages:
                        return [], failed(plan, resolver=plan.strategy, error="Website unreachable")
        if not pages:
            return [], await _no_pages_reason(plan, company)
        return pages, None

    # ------------------------------------------------------------------ per entity
    async def run(self, entity_ids: Sequence[uuid.UUID]) -> dict[str, int]:
        async def guarded(eid: uuid.UUID) -> None:
            async with self.sem:
                try:
                    await self._process(eid)
                except Exception as exc:
                    log.exception("enrich.entity_failed", entity_id=str(eid))
                    await self._write(
                        eid,
                        failed(self.plan, resolver=self.plan.strategy, error=_human_error(exc)),
                        None,
                        None,
                    )

        self.learning = await EnrichLearning.start()
        try:
            await asyncio.gather(*(guarded(eid) for eid in entity_ids))
        finally:
            await self._flush_events(force=True)
            await self.learning.flush()
        return self.stats

    async def _process(self, eid: uuid.UUID) -> None:
        plan = self.plan
        cell = self.cells.get(eid)
        if cell is not None and cell.is_user_override:
            self.stats["skipped"] += 1
            return
        person = self.people.get(eid) if self.etype == EntityType.person else None
        company = (
            self.companies.get(eid)
            if self.etype == EntityType.company
            else self.companies.get(person.company_id)
            if person is not None and person.company_id
            else None
        )
        if (self.etype == EntityType.company and company is None) or (
            self.etype == EntityType.person and person is None
        ):
            await self._write(
                eid, failed(plan, resolver=plan.strategy, error="Lead no longer exists"), None, None
            )
            return
        stale_by_age = bool(
            cell is not None and cell.observed_at is not None and cell.observed_at < self.cutoff
        )
        refreshing = (
            self.force
            or self.refresh
            or stale_by_age
            or (cell is not None and cell.status == CellStatus.stale)
        )

        pages: list[WebsitePage] = []
        if company is not None and (plan.strategy in WEBSITE_STRATEGIES or plan.strategy == "tech_detection"):
            pages, problem = await self.pages_for(company, refresh=refreshing)
            if problem is not None and (plan.strategy in STRICT_WEBSITE or not company.description):
                problem.input_hash = None
                await self._write(eid, problem, company, person)
                return
        elif company is None and plan.strategy in STRICT_WEBSITE:
            await self._write(
                eid, unknown(plan, resolver=plan.strategy, error="Missing dependency: company"), None, person
            )
            return

        factual = self.factual.get(eid, {})
        deps = self.deps.get(eid, {})
        input_hash = compute_input_hash(plan, pages, _extra_inputs(plan, company, person, factual, deps))
        if (
            not self.force
            and cell is not None
            and cell.input_hash == input_hash
            and plan.strategy in SKIPPABLE
            and cell.status
            in (
                CellStatus.success,
                CellStatus.unknown,
                CellStatus.stale,
                CellStatus.queued,
                CellStatus.running,
            )
            and not (plan.strategy == "web_research" and (stale_by_age or self.refresh))
        ):
            await self._restore(eid, cell)
            return

        await self._set_running(eid)
        rc = ResolveContext(
            plan=plan,
            workspace_id=self.ws,
            company=company,
            person=person,
            pages=pages,
            column_id=self.column.id,
            factual_values=factual,
            dependency_values=deps,
            force=self.force,
        )
        t0 = time.monotonic()
        result = await _safe_resolve(rc)
        self.learning.observe(plan.strategy, result, t0)
        result.input_hash = input_hash if result.status != CellStatus.failed else None
        await self._write(eid, result, company, person)

    # ------------------------------------------------------------------ writes
    async def _restore(self, eid: uuid.UUID, cell: CustomFieldValue) -> None:
        """Inputs unchanged: keep the previous result (no recomputation), refresh its freshness."""
        status = CellStatus.success if cell.value_json is not None else CellStatus.unknown
        self.stats["unchanged"] += 1
        if cell.status == status and not (cell.observed_at is not None and cell.observed_at < self.cutoff):
            return
        async with session_scope() as s:
            await s.execute(
                sa.update(CustomFieldValue)
                .where(
                    CustomFieldValue.id == cell.id,
                    CustomFieldValue.is_user_override.is_(False),
                )
                .values(status=status, observed_at=sa.func.now(), updated_at=sa.func.now())
            )
        await self._event(eid, status, cell.display_value, cell.confidence)

    async def _set_running(self, eid: uuid.UUID) -> None:
        async with session_scope() as s:
            await _upsert_status(s, self.ws, self.column.id, self.etype, [eid], CellStatus.running)
        await self._event(eid, CellStatus.running, None, None)

    async def _write(
        self, eid: uuid.UUID, result: CellResult, company: Company | None, person: Person | None
    ) -> None:
        status = CellStatus(result.status)
        values: dict[str, Any] = {
            "value_json": sa.null() if result.value is None else result.value,
            "display_value": result.display_value
            if result.display_value is not None
            else display_for(result.value),
            "confidence": result.confidence,
            "source_id": result.source_id,
            "source_url": result.source_url,
            "evidence": result.evidence[:4000] if result.evidence else None,
            "resolver": result.resolver or self.plan.strategy,
            "status": status,
            "error": result.error,
            "input_hash": result.input_hash,
            "model": result.model,
            "cost_usd": Decimal(str(round(result.cost_usd or 0.0, 6))),
            "observed_at": sa.func.now(),
        }
        async with session_scope() as s:
            stmt = pg_insert(CustomFieldValue).values(
                id=uuid7(),
                workspace_id=self.ws,
                column_id=self.column.id,
                entity_type=self.etype,
                entity_id=eid,
                **values,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["column_id", "entity_type", "entity_id"],
                set_={**values, "updated_at": sa.func.now()},
                where=CustomFieldValue.is_user_override.is_(False),
            )
            await s.execute(stmt)
            if status in (CellStatus.success, CellStatus.unknown):
                company_id = company.id if company is not None else None
                s.add(
                    LeadExposure(
                        workspace_id=self.ws,
                        entity_type=self.etype,
                        entity_id=eid,
                        company_id=company_id,
                        exposure_type=ExposureType.ENRICHED,
                        campaign_id=self.campaign_id,
                    )
                )
                if (
                    self.etype == EntityType.person
                    and company_id
                    and company_id not in self._exposed_companies
                ):
                    self._exposed_companies.add(company_id)
                    s.add(
                        LeadExposure(
                            workspace_id=self.ws,
                            entity_type=EntityType.company,
                            entity_id=company_id,
                            company_id=company_id,
                            exposure_type=ExposureType.ENRICHED,
                            campaign_id=self.campaign_id,
                        )
                    )
                model = Company if self.etype == EntityType.company else Person
                await s.execute(
                    sa.update(model).where(model.id == eid).values(last_enriched_at=sa.func.now())
                )
        self.stats["processed"] += 1
        self.stats[status.value if status.value in self.stats else "failed"] += 1
        await self._event(eid, status, values["display_value"], result.confidence)

    async def _event(
        self, eid: uuid.UUID, status: CellStatus, display: str | None, confidence: float | None
    ) -> None:
        async with self._event_lock:
            self._events.append(
                {
                    "entity_type": self.etype.value,
                    "entity_id": str(eid),
                    "status": status.value,
                    "display_value": display,
                    "confidence": confidence,
                }
            )
        await self._flush_events()

    async def _flush_events(self, *, force: bool = False) -> None:
        from scout.jobs.events import emit

        async with self._event_lock:
            if not self._events or (not force and len(self._events) < EVENT_FLUSH_EVERY):
                return
            cells, self._events = self._events, []
        await emit(
            self.ws,
            "cell.updated",
            {"column_id": str(self.column.id), "cells": cells},
            campaign_id=self.campaign_id,
            job_id=self.job_id,
        )


async def run_batch(
    workspace_id: uuid.UUID,
    column_id: uuid.UUID,
    entity_ids: Sequence[uuid.UUID],
    *,
    force: bool = False,
    refresh: bool = False,
    max_page_age_days: int | None = None,
    campaign_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Compute cells for `entity_ids` (the body of an `enrichment.batch` job; also callable inline)."""
    async with session_scope() as s:
        col = await s.scalar(
            sa.select(CustomColumn).where(
                CustomColumn.id == column_id, CustomColumn.workspace_id == workspace_id
            )
        )
    if col is None:
        return {"skipped": "column_deleted"}
    if checkpoint is not None:
        await checkpoint()  # cooperative campaign pause/cancel before any work
    batch = _Batch(
        workspace_id,
        col,
        force=force,
        refresh=refresh,
        max_page_age_days=max_page_age_days,
        campaign_id=campaign_id,
        job_id=job_id,
    )
    ids = list(dict.fromkeys(entity_ids))
    await batch.load(ids)
    stats = await batch.run(ids)
    await _emit_progress(workspace_id, column_id, campaign_id=campaign_id, job_id=job_id)
    log.info("enrich.batch_done", column_id=str(column_id), **stats)
    return {"column_id": str(column_id), **stats}


# =============================================================================================
# Overrides & freshness
# =============================================================================================
async def set_user_value(
    workspace_id: uuid.UUID,
    column_id: uuid.UUID,
    entity_type: EntityType | str,
    entity_id: uuid.UUID,
    value: Any,
    *,
    user_id: str,
) -> CustomFieldValue:
    """Store a user-entered value (is_user_override, source "user", confidence 1.0). It is never overwritten
    by automatic enrichment. `value=None` clears the override (the cell becomes `not_started`)."""
    etype = EntityType(str(getattr(entity_type, "value", entity_type)))
    async with session_scope() as s:
        col = await _get_column(s, workspace_id, column_id)
        if value is None:
            values: dict[str, Any] = {
                "value_json": sa.null(),
                "display_value": None,
                "confidence": None,
                "source_id": None,
                "source_url": None,
                "evidence": None,
                "resolver": None,
                "status": CellStatus.not_started,
                "error": None,
                "input_hash": None,
                "is_user_override": False,
                "model": None,
                "cost_usd": Decimal("0"),
                "observed_at": None,
            }
        else:
            coerced = coerce_user_value(value, col.data_type, plan_of(col).enum_values)
            # Empirical Source Scoring: the replaced value was wrong (or confirmed) for the column's resolver.
            prev = await s.scalar(
                sa.select(CustomFieldValue).where(
                    CustomFieldValue.column_id == column_id,
                    CustomFieldValue.entity_type == etype,
                    CustomFieldValue.entity_id == entity_id,
                )
            )
            await learning_feedback.cell_overridden(s, plan_of(col).strategy, prev, coerced)
            values = {
                "value_json": coerced,
                "display_value": display_for(coerced, col.data_type),
                "confidence": 1.0,
                "source_id": "user",
                "source_url": None,
                "evidence": f"Entered by {user_id}",
                "resolver": "user",
                "status": CellStatus.success,
                "error": None,
                "input_hash": None,
                "is_user_override": True,
                "model": None,
                "cost_usd": Decimal("0"),
                "observed_at": sa.func.now(),
            }
        stmt = (
            pg_insert(CustomFieldValue)
            .values(
                id=uuid7(),
                workspace_id=workspace_id,
                column_id=column_id,
                entity_type=etype,
                entity_id=entity_id,
                **values,
            )
            .on_conflict_do_update(
                index_elements=["column_id", "entity_type", "entity_id"],
                set_={**values, "updated_at": sa.func.now()},
            )
            .returning(CustomFieldValue.id)
        )
        cell_id = (await s.execute(stmt)).scalar_one()
        cell = await s.get(CustomFieldValue, cell_id, populate_existing=True)
    assert cell is not None
    return cell


async def mark_stale_cells(workspace_id: uuid.UUID) -> int:
    """Mark success/unknown cells older than their column's `refresh_days` as stale. Returns the count."""
    async with session_scope() as s:
        res = await s.execute(
            sa.text(
                """
                UPDATE custom_field_values AS v SET status = 'stale', updated_at = now()
                FROM custom_columns AS c
                WHERE v.column_id = c.id AND c.workspace_id = :ws AND v.workspace_id = :ws
                  AND v.status IN ('success', 'unknown') AND NOT v.is_user_override
                  AND v.observed_at IS NOT NULL
                  AND v.observed_at < now() - make_interval(
                        days => COALESCE(NULLIF(c.refresh_policy->>'refresh_days', '')::int, 30))
                """
            ),
            {"ws": workspace_id},
        )
        return int(res.rowcount or 0)  # type: ignore[attr-defined]


async def refresh_column(
    workspace_id: uuid.UUID,
    column_id: uuid.UUID,
    *,
    older_than_days: int | None = None,
    entity_ids: Sequence[uuid.UUID] | None = None,
) -> int:
    """Mark matching non-override cells stale and re-enqueue them (pages older than `older_than_days` are
    recrawled; results recompute only where inputs changed, except web research which always re-runs)."""
    async with session_scope() as s:
        col = await _get_column(s, workspace_id, column_id)
        stmt = (
            sa.update(CustomFieldValue)
            .where(
                CustomFieldValue.column_id == column_id,
                CustomFieldValue.workspace_id == workspace_id,
                CustomFieldValue.is_user_override.is_(False),
                CustomFieldValue.status.in_(
                    [CellStatus.success, CellStatus.unknown, CellStatus.failed, CellStatus.stale]
                ),
            )
            .values(status=CellStatus.stale, updated_at=sa.func.now())
            .returning(CustomFieldValue.entity_id)
        )
        if older_than_days is not None:
            stmt = stmt.where(
                sa.or_(
                    CustomFieldValue.observed_at.is_(None),
                    CustomFieldValue.observed_at < datetime.now(UTC) - timedelta(days=int(older_than_days)),
                )
            )
        if entity_ids is not None:
            stmt = stmt.where(CustomFieldValue.entity_id.in_(list(entity_ids)))
        stale_ids = list((await s.execute(stmt)).scalars().all())
    targets = list(dict.fromkeys([*stale_ids, *(entity_ids or [])]))
    if not targets:
        return 0
    return await enqueue_column(
        workspace_id,
        col.id,
        entity_ids=targets,
        only_missing=True,
        refresh=True,
        max_page_age_days=older_than_days,
    )
