"""Technology detection for a company (run only when requested by a column/condition).

Uses the cached home page (headers, head HTML, text, links) — crawling only when no home page is
cached — tries the wappalyzergo service, falls back to built-in signatures, then upserts
``technologies`` rows and records ``company_field_observations`` (source_type=tech_scan).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scout.crawl.cache import ensure_crawled
from scout.db.engine import session_scope
from scout.db.enums import ErrorCategory, PageType, SourceType
from scout.db.models import Company, CompanyFieldObservation, Technology, WebsitePage
from scout.errors import PermanentError
from scout.tech import builtin
from scout.tech.service import detect_via_service
from scout.tech.types import DetectedTech

log = structlog.get_logger(__name__)


async def _technologies(company_id: uuid.UUID) -> list[Technology]:
    async with session_scope() as s:
        rows = (
            await s.scalars(sa.select(Technology).where(Technology.company_id == company_id).order_by(Technology.name))
        ).all()
    return list(rows)


async def _home_page(workspace_id: uuid.UUID, company_id: uuid.UUID) -> WebsitePage | None:
    async with session_scope() as s:
        return await s.scalar(
            sa.select(WebsitePage)
            .where(
                WebsitePage.workspace_id == workspace_id,
                WebsitePage.company_id == company_id,
                WebsitePage.page_type == PageType.home,
            )
            .order_by(WebsitePage.fetched_at.desc())
            .limit(1)
        )


async def detect_technologies(
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    *,
    force: bool = False,
    max_age_days: int = 30,
) -> list[Technology]:
    """Current technologies of a company (cached rows when fresh, else a new scan)."""
    now = datetime.now(UTC)
    async with session_scope() as s:
        exists = await s.scalar(
            sa.select(Company.id).where(Company.id == company_id, Company.workspace_id == workspace_id)
        )
        if exists is None:
            raise PermanentError(f"company {company_id} not found", category=ErrorCategory.not_found)
        latest = await s.scalar(sa.select(sa.func.max(Technology.observed_at)).where(Technology.company_id == company_id))
    if not force and latest is not None and latest >= now - timedelta(days=max_age_days):
        return await _technologies(company_id)

    home = await _home_page(workspace_id, company_id)
    if home is None:
        await ensure_crawled(workspace_id, company_id)
        home = await _home_page(workspace_id, company_id)
    if home is None:
        log.info("tech_no_home_page", company_id=str(company_id))
        return await _technologies(company_id)

    headers = home.response_headers or {}
    detected: list[DetectedTech] | None = await detect_via_service(home.url, headers, home.head_html)
    detector = "wappalyzergo"
    if not detected:
        detected = builtin.detect(headers, home.head_html, home.content_text, home.links)
        detector = "builtin"

    async with session_scope() as s:
        names = [t.name for t in detected]
        # Technologies no longer present on the site are removed; history stays in observations.
        await s.execute(
            sa.delete(Technology).where(
                Technology.company_id == company_id, Technology.name.not_in(names) if names else sa.true()
            )
        )
        for tech in detected:
            values = {
                "category": tech.category,
                "version": tech.version,
                "confidence": tech.confidence,
                "detector": detector,
                "source_url": home.url,
                "observed_at": now,
            }
            stmt = pg_insert(Technology).values(
                workspace_id=workspace_id, company_id=company_id, name=tech.name, **values
            )
            await s.execute(stmt.on_conflict_do_update(index_elements=["company_id", "name"], set_=values))
        await s.execute(
            sa.update(CompanyFieldObservation)
            .where(
                CompanyFieldObservation.company_id == company_id,
                CompanyFieldObservation.field_name == "technology",
                CompanyFieldObservation.is_current.is_(True),
            )
            .values(is_current=False)
        )
        for tech in detected:
            s.add(
                CompanyFieldObservation(
                    workspace_id=workspace_id,
                    company_id=company_id,
                    field_name="technology",
                    value_json={"name": tech.name, "category": tech.category, "version": tech.version, "detector": detector},
                    source_type=SourceType.tech_scan,
                    source_key="tech_scan",
                    source_url=home.url,
                    page_id=home.id,
                    evidence=tech.evidence[:1000],
                    confidence=tech.confidence,
                    is_current=True,
                    observed_at=now,
                )
            )
    log.info("tech_detected", company_id=str(company_id), detector=detector, count=len(detected))
    return await _technologies(company_id)
