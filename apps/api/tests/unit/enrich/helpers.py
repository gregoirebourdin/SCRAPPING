"""Fixture loading for enrichment tests (unit: transient ORM objects; integration: persisted rows)."""

from __future__ import annotations

import json
import types
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scout.db.enums import PageType
from scout.db.models import Company, WebsitePage
from scout.util.text import content_hash, normalize_company_name

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "enrich"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def make_company(data: dict[str, Any], *, workspace_id: uuid.UUID | None = None) -> Company:
    c = data["company"]
    return Company(
        id=uuid.uuid4(),
        workspace_id=workspace_id or uuid.uuid4(),
        name=c["name"],
        normalized_name=normalize_company_name(c["name"]),
        domain=c.get("domain"),
        normalized_domain=c.get("domain"),
        website_url=c.get("website_url"),
        city=c.get("city"),
        country=c.get("country"),
        industry=c.get("industry"),
        employee_min=c.get("employee_min"),
        employee_max=c.get("employee_max"),
        description=c.get("description"),
    )


def make_pages(data: dict[str, Any], company: Company) -> list[WebsitePage]:
    pages = []
    for p in data["pages"]:
        pages.append(
            WebsitePage(
                id=uuid.uuid4(),
                workspace_id=company.workspace_id,
                company_id=company.id,
                url=p["url"],
                canonical_url=p["url"],
                page_type=PageType(p["page_type"]),
                title=p.get("title"),
                meta_description=p.get("meta_description"),
                content_text=p["content_text"],
                content_hash=content_hash(p["content_text"]),
                language=p.get("language"),
                head_html=p.get("head_html"),
                links=p.get("links") or {},
                emails=p.get("emails") or [],
                phones=p.get("phones") or [],
                structured_data=p.get("structured_data") or [],
                headings=[],
                fetched_at=datetime.now(UTC),
            )
        )
    return pages


def fake_crawl_module(state: dict[str, Any] | None = None) -> types.ModuleType:
    """Stand-in for `scout.crawl.cache` (written concurrently by another team): serves pages from the DB."""
    import sqlalchemy as sa

    from scout.db.engine import session_scope

    mod = types.ModuleType("scout.crawl.cache")
    state = state if state is not None else {}
    state.setdefault("ensure_calls", [])

    async def get_cached_pages(workspace_id: uuid.UUID, company_id: uuid.UUID) -> list[WebsitePage]:
        async with session_scope() as s:
            rows = await s.scalars(
                sa.select(WebsitePage).where(
                    WebsitePage.workspace_id == workspace_id, WebsitePage.company_id == company_id
                )
            )
            return list(rows.all())

    async def ensure_crawled(
        workspace_id: uuid.UUID,
        company_id: uuid.UUID,
        *,
        max_age_days: int = 30,
        force: bool = False,
        max_pages: int | None = None,
    ) -> list[WebsitePage]:
        state["ensure_calls"].append(company_id)
        return await get_cached_pages(workspace_id, company_id)

    mod.get_cached_pages = get_cached_pages  # type: ignore[attr-defined]
    mod.ensure_crawled = ensure_crawled  # type: ignore[attr-defined]
    mod.state = state  # type: ignore[attr-defined]
    return mod


def sentence_containing(data: dict[str, Any], needle: str) -> tuple[str, str]:
    """(sentence, page url) for the first fixture line containing `needle` — a valid evidence quote."""
    for p in data["pages"]:
        for line in p["content_text"].splitlines():
            if needle in line:
                return line, p["url"]
    raise KeyError(needle)


async def seed_agencies(workspace_id: uuid.UUID) -> dict[str, Any]:
    """Persist both fixture agencies with their pages, three people and a list containing the people.

    Returns ids: {"lumiere", "ruche", "list", "people": {"claire", "paul", "thomas"}}.
    """
    from scout.db.engine import session_scope
    from scout.db.enums import Seniority, WebsiteStatus
    from scout.db.models import List, ListMembership, Person
    from scout.util.text import normalize_person_name

    out: dict[str, Any] = {"people": {}}
    async with session_scope() as s:
        for key, fixture in (("lumiere", "agency_social_follow"), ("ruche", "agency_instagram")):
            data = load_fixture(fixture)
            company = make_company(data, workspace_id=workspace_id)
            company.website_status = WebsiteStatus.ok
            s.add(company)
            await s.flush()
            s.add_all(make_pages(data, company))
            out[key] = company.id
        people = [
            ("claire", "Claire Martin", "Fondatrice & CEO", out["lumiere"], Seniority.owner),
            ("paul", "Paul Petit", None, out["lumiere"], None),
            ("thomas", "Thomas Bernard", "Directeur général", out["ruche"], Seniority.c_level),
        ]
        for key, name, title, company_id, seniority in people:
            p = Person(
                workspace_id=workspace_id,
                company_id=company_id,
                full_name=name,
                normalized_name=normalize_person_name(name),
                job_title=title,
                seniority=seniority,
            )
            s.add(p)
            await s.flush()
            out["people"][key] = p.id
        lst = List(workspace_id=workspace_id, name="Agencies")
        s.add(lst)
        await s.flush()
        out["list"] = lst.id
        for pid in out["people"].values():
            s.add(ListMembership(workspace_id=workspace_id, list_id=lst.id, person_id=pid))
    return out
