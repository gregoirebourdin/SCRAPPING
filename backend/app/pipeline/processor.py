"""Turn one URL into a fully extracted + scored agency record (crawl → extract → qualify)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..extract.clients import ClientHit, extract_clients
from ..extract.company import CompanyInfo, extract_company
from ..extract.contacts import Contacts, extract_contacts
from ..extract.tech import detect_tech
from ..enrich.founder import FounderHit, company_linkedin, extract_founder
from ..fetch.crawler import CrawledSite, SiteCrawler
from ..models import Agency, AgencyEmail, Client
from ..qualify.language import detect_language
from ..qualify.liveness import Liveness, assess_liveness
from ..qualify.scoring import ScoreResult, score_agency
from ..util.urls import registrable_domain

log = logging.getLogger(__name__)


@dataclass
class ProcessedSite:
    site: CrawledSite
    liveness: Liveness
    language: tuple[str, float] = ("und", 0.0)
    company: CompanyInfo | None = None
    contacts: Contacts = field(default_factory=Contacts)
    tech: list[str] = field(default_factory=list)
    clients: list[ClientHit] = field(default_factory=list)
    founder: FounderHit = field(default_factory=FounderHit)
    score: ScoreResult | None = None

    @property
    def key_pages(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in self.site.pages:
            out.setdefault(p.kind, p.url)
        return out


async def process_site(url: str, crawler: SiteCrawler, *, min_score: int | None = None) -> ProcessedSite:
    site = await crawler.crawl(url)
    company = extract_company(site) if site.alive else None
    liveness = assess_liveness(site, company.copyright_year if company else None)
    out = ProcessedSite(site=site, liveness=liveness, company=company)
    if not liveness.alive or site.home is None:
        out.score = ScoreResult(0, "rejected", None, f"dead:{liveness.reason}", [], [], {"gate": "liveness"})
        return out
    sample_pages = site.pages_of("home", "about", "services")
    out.language = detect_language("\n".join(p.text for p in sample_pages)[:12_000], site.home.parsed.lang_attr)
    out.tech = detect_tech(site.all_html)
    out.contacts = extract_contacts(site)
    if out.language[0] == "en":
        out.clients = extract_clients(site, company.name if company else site.domain)
        out.founder = extract_founder(site)
        if not out.contacts.socials.get("linkedin"):
            li = company_linkedin(site)
            if li:
                out.contacts.socials["linkedin"] = li
    out.score = score_agency(site, liveness, out.language, out.clients, out.contacts, min_score=min_score)
    return out


async def persist_agency(session: AsyncSession, processed: ProcessedSite, *, candidate_id: int | None, run_id: int | None) -> Agency:
    """Upsert the agency row (+ emails + clients) from a processed site."""
    site, sc = processed.site, processed.score
    assert sc is not None
    domain = site.domain
    res = await session.execute(select(Agency).where(Agency.domain == domain))
    agency = res.scalar_one_or_none()
    if agency is None:
        agency = Agency(domain=domain, website=site.final_url or site.home_url)
        session.add(agency)
        await session.flush()
    agency.candidate_id = candidate_id
    agency.run_id = run_id
    agency.website = site.final_url or site.home_url
    c = processed.company
    if c:
        agency.name = c.name
        agency.tagline = c.tagline
        agency.description = c.description
        agency.country = c.country
        agency.city = c.city
        agency.founded_year = c.founded_year
        agency.team_size_hint = c.team_size_hint
    agency.language, agency.language_confidence = processed.language[0], round(processed.language[1], 3)
    agency.services = sc.services
    agency.icp_signals = sc.icp_signals
    agency.tech = processed.tech
    agency.socials = processed.contacts.socials
    agency.phones = processed.contacts.phones
    agency.booking_url = processed.contacts.booking_url
    agency.alive = processed.liveness.alive
    agency.alive_details = processed.liveness.details
    agency.last_activity = processed.liveness.last_activity
    agency.pages_crawled = len(site.pages)
    agency.key_pages = processed.key_pages
    agency.score = sc.score
    agency.score_breakdown = sc.breakdown
    agency.tier = sc.tier
    agency.status = sc.status
    agency.reject_reason = sc.reject_reason
    f = processed.founder
    serp_resolved = agency.founder_source == "serp" and agency.founder_linkedin
    if f.name or f.linkedin or not serp_resolved:
        # always reflect the latest extraction (a reprocess must be able to *remove* a wrong founder),
        # except that a LinkedIn URL resolved through search engines is kept when the site itself says nothing
        agency.founder_name = f.name
        agency.founder_title = f.title
        agency.founder_linkedin = f.linkedin
        agency.founder_source = f.source if (f.name or f.linkedin) else None
        agency.founder_confidence = f.confidence if (f.name or f.linkedin) else None
    agency.enrich_stage = "none"

    # emails: replace, but keep verification results already obtained for the same address
    old = {e.email: e for e in (await session.execute(select(AgencyEmail).where(AgencyEmail.agency_id == agency.id))).scalars()}
    await session.execute(delete(AgencyEmail).where(AgencyEmail.agency_id == agency.id))
    for i, h in enumerate(processed.contacts.emails):
        prev = old.get(h.email)
        session.add(AgencyEmail(
            agency_id=agency.id, email=h.email, source=h.source, page_url=h.page_url, confidence=round(h.confidence, 3),
            rank=i, is_primary=(i == 0), verification=prev.verification if prev else "unknown", verified_at=prev.verified_at if prev else None,
        ))
    # clients: replace
    await session.execute(delete(Client).where(Client.agency_id == agency.id))
    seen: set[str] = set()
    for h in processed.clients:
        key = h.name.lower()
        if key in seen:
            continue
        seen.add(key)
        session.add(Client(
            agency_id=agency.id, name=h.name, kind=h.kind, role_title=h.role_title, niche=h.niche, evidence=h.evidence,
            evidence_url=h.evidence_url, website=h.website, website_source=h.website_source, confidence=round(h.confidence, 3), status="found",
        ))
    await session.flush()
    return agency


def summarize(processed: ProcessedSite) -> dict:
    """Compact dict for logs / the ``test-site`` CLI command."""
    sc = processed.score
    return {
        "domain": processed.site.domain,
        "alive": processed.liveness.alive,
        "liveness": processed.liveness.reason,
        "language": processed.language,
        "name": processed.company.name if processed.company else None,
        "country": processed.company.country if processed.company else None,
        "pages": [(p.kind, p.url) for p in processed.site.pages],
        "emails": [(e.email, e.source, round(e.confidence, 2)) for e in processed.contacts.emails],
        "socials": processed.contacts.socials,
        "booking": processed.contacts.booking_url,
        "tech": processed.tech,
        "founder": vars(processed.founder),
        "clients": [(c.name, c.kind, c.role_title, round(c.confidence, 2), c.website, sorted(c.signals)) for c in processed.clients],
        "score": sc.score if sc else None,
        "status": sc.status if sc else None,
        "tier": sc.tier if sc else None,
        "reject_reason": sc.reject_reason if sc else None,
        "breakdown": sc.breakdown if sc else None,
    }


__all__ = ["ProcessedSite", "process_site", "persist_agency", "summarize", "registrable_domain"]
