"""Website cache (``website_crawl_runs`` / ``website_pages``): crawl once per domain, reuse everywhere.

``ensure_crawled`` returns cached pages without any network access while they are fresh; otherwise
it crawls (no DB transaction is held during network I/O), records a crawl run, upserts pages
(keeping ``content_hash`` stable for unchanged content) and updates the company's website status.
"""

from __future__ import annotations

import asyncio
import uuid
import weakref
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from scout.crawl.crawler import crawl_site
from scout.crawl.types import CrawlResult, FetchedPage
from scout.db.engine import session_scope
from scout.db.enums import ErrorCategory, PageType, WebsiteStatus
from scout.db.models import Company, WebsiteCrawlRun, WebsitePage
from scout.errors import PermanentError
from scout.services.usage import current_usage_context
from scout.util.urls import normalize_website, registrable_domain

log = structlog.get_logger(__name__)

# Unreachable / parked / blocked sites are not re-crawled more often than this.
NEGATIVE_CACHE_DAYS = 7
_PAGE_TYPE_ORDER = {pt.value: i for i, pt in enumerate(PageType)}


def _now() -> datetime:
    return datetime.now(UTC)


async def get_cached_pages(workspace_id: uuid.UUID, company_id: uuid.UUID) -> list[WebsitePage]:
    """All cached pages of a company (home first, then by page type and URL)."""
    async with session_scope() as s:
        rows = (
            await s.scalars(
                sa.select(WebsitePage).where(
                    WebsitePage.workspace_id == workspace_id, WebsitePage.company_id == company_id
                )
            )
        ).all()
    return sorted(rows, key=lambda p: (_PAGE_TYPE_ORDER.get(str(p.page_type), 99), p.url))


def _page_values(page: FetchedPage, *, now: datetime, run_id: uuid.UUID) -> dict[str, Any]:
    return {
        "url": page.final_url or page.url,
        "page_type": page.page_type,
        "title": page.title,
        "meta_description": page.meta_description,
        "content_text": page.content_text,
        "content_hash": page.content_hash,
        "status_code": page.status_code,
        "fetched_at": now,
        "etag": page.etag,
        "last_modified": page.last_modified,
        "content_type": page.content_type,
        "language": page.language,
        "fetch_tier": page.fetch_tier,
        "head_html": page.head_html,
        "response_headers": page.headers or None,
        "links": page.links or {},
        "emails": page.emails or [],
        "phones": page.phones or [],
        "structured_data": page.structured_data or [],
        "headings": page.headings or [],
        "word_count": page.word_count,
        "crawl_run_id": run_id,
    }


async def _start_run(workspace_id: uuid.UUID, company_id: uuid.UUID, domain: str) -> uuid.UUID:
    ctx = current_usage_context()
    async with session_scope() as s:
        run = WebsiteCrawlRun(
            workspace_id=workspace_id,
            company_id=company_id,
            domain=domain,
            status="running",
            job_id=ctx.job_id if ctx else None,
        )
        s.add(run)
        await s.flush()
        return run.id


async def _persist(
    workspace_id: uuid.UUID,
    company: Company,
    run_id: uuid.UUID,
    result: CrawlResult,
    cached: dict[str, WebsitePage],
) -> None:
    now = _now()
    unchanged = 0
    async with session_scope() as s:
        for page in result.pages:
            existing = cached.get(page.canonical_url)
            if page.not_modified:
                unchanged += 1
                await s.execute(
                    sa.update(WebsitePage)
                    .where(
                        WebsitePage.company_id == company.id, WebsitePage.canonical_url == page.canonical_url
                    )
                    .values(
                        fetched_at=now,
                        crawl_run_id=run_id,
                        etag=page.etag or (existing.etag if existing else None),
                        last_modified=page.last_modified or (existing.last_modified if existing else None),
                    )
                )
                continue
            if existing is not None and existing.content_hash == page.content_hash:
                unchanged += 1
            values = _page_values(page, now=now, run_id=run_id)
            stmt = pg_insert(WebsitePage).values(
                workspace_id=workspace_id, company_id=company.id, canonical_url=page.canonical_url, **values
            )
            stmt = stmt.on_conflict_do_update(index_elements=["company_id", "canonical_url"], set_=values)
            await s.execute(stmt)

        await s.execute(
            sa.update(WebsiteCrawlRun)
            .where(WebsiteCrawlRun.id == run_id)
            .values(
                status=result.status.value,
                tier_max=result.tier_max,
                pages_fetched=len(result.pages),
                pages_failed=result.pages_failed,
                pages_unchanged=unchanged,
                bytes=result.bytes,
                error_category=result.error_category,
                error=result.error,
                finished_at=now,
                domain=result.domain or company.normalized_domain or "",
            )
        )

        updates: dict[str, Any] = {"website_status": result.status, "last_crawled_at": now}
        if result.status == WebsiteStatus.ok and result.home_url:
            updates["website_url"] = normalize_website(result.home_url) or company.website_url
        await s.execute(sa.update(Company).where(Company.id == company.id).values(**updates))

        domain = registrable_domain(result.home_url) if result.home_url else None
        if result.status == WebsiteStatus.ok and domain and company.normalized_domain is None:
            owner = await s.scalar(
                sa.select(Company.id).where(
                    Company.workspace_id == workspace_id,
                    Company.normalized_domain == domain,
                    Company.id != company.id,
                )
            )
            if owner is None:
                try:
                    async with s.begin_nested():
                        await s.execute(
                            sa.update(Company)
                            .where(Company.id == company.id, Company.normalized_domain.is_(None))
                            .values(normalized_domain=domain, domain=company.domain or domain)
                        )
                except IntegrityError:
                    log.info("crawl_domain_taken", company_id=str(company.id), domain=domain)
            else:
                log.info(
                    "crawl_domain_owned_by_other", company_id=str(company.id), domain=domain, owner=str(owner)
                )


# One crawl per company at a time in this process: concurrent campaigns / columns needing the same site
# wait for the first crawl and then reuse its cache instead of fetching the site twice.
_crawl_locks: weakref.WeakValueDictionary[uuid.UUID, asyncio.Lock] = weakref.WeakValueDictionary()


def _crawl_lock(company_id: uuid.UUID) -> asyncio.Lock:
    lock = _crawl_locks.get(company_id)
    if lock is None:
        lock = asyncio.Lock()
        _crawl_locks[company_id] = lock
    return lock


async def ensure_crawled(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    *,
    max_age_days: int = 30,
    force: bool = False,
    max_pages: int | None = None,
) -> list[WebsitePage]:
    """Return the company's cached pages, crawling first when the cache is missing or stale.

    Site-level failures (unreachable / parked / blocked) are recorded, not raised; the cached
    pages (often none) are returned. Unexpected database errors propagate.
    """
    lock = _crawl_lock(company_id)
    async with lock:
        return await _ensure_crawled(
            workspace_id, company_id, max_age_days=max_age_days, force=force, max_pages=max_pages
        )


async def _ensure_crawled(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    *,
    max_age_days: int,
    force: bool,
    max_pages: int | None,
) -> list[WebsitePage]:
    now = _now()
    async with session_scope() as s:
        company = await s.scalar(
            sa.select(Company).where(Company.id == company_id, Company.workspace_id == workspace_id)
        )
        if company is None:
            raise PermanentError(f"company {company_id} not found", category=ErrorCategory.not_found)
        latest = await s.scalar(
            sa.select(sa.func.max(WebsitePage.fetched_at)).where(WebsitePage.company_id == company_id)
        )
        cached_rows = (
            await s.scalars(sa.select(WebsitePage).where(WebsitePage.company_id == company_id))
        ).all()
        if not company.website_url:
            if company.website_status != WebsiteStatus.none:
                company.website_status = WebsiteStatus.none
            return []

    if not force:
        if latest is not None and latest >= now - timedelta(days=max_age_days):
            return await get_cached_pages(workspace_id, company_id)
        negative_days = min(max_age_days, NEGATIVE_CACHE_DAYS)
        if (
            company.website_status in (WebsiteStatus.unreachable, WebsiteStatus.parked, WebsiteStatus.blocked)
            and company.last_crawled_at is not None
            and company.last_crawled_at >= now - timedelta(days=negative_days)
        ):
            return []

    cached = {p.canonical_url: p for p in cached_rows}
    known = {p.canonical_url: (p.etag, p.last_modified, p.content_hash) for p in cached_rows}
    website_url = company.website_url
    run_id = await _start_run(
        workspace_id, company_id, company.normalized_domain or registrable_domain(website_url) or website_url
    )
    try:
        result = await crawl_site(website_url, max_pages=max_pages, known=known)
    except Exception as exc:
        async with session_scope() as s:
            await s.execute(
                sa.update(WebsiteCrawlRun)
                .where(WebsiteCrawlRun.id == run_id)
                .values(
                    status="error",
                    error=repr(exc)[:500],
                    error_category=ErrorCategory.internal,
                    finished_at=_now(),
                )
            )
        raise
    await _persist(workspace_id, company, run_id, result, cached)
    log.info(
        "website_crawled",
        company_id=str(company_id),
        status=result.status.value,
        pages=len(result.pages),
        failed=result.pages_failed,
    )
    if result.status != WebsiteStatus.ok:
        return []
    return await get_cached_pages(workspace_id, company_id)
