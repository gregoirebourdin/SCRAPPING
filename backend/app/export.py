"""CSV export of leads (one row per agency, best client + best funnel flattened)."""

from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .db import SessionLocal
from .models import Agency, Client

COLUMNS = [
    "tier", "score", "status", "agency_name", "website", "domain", "email", "email_verification", "email_source", "all_emails",
    "founder_name", "founder_title", "founder_linkedin", "founder_source", "linkedin_company", "instagram", "facebook", "youtube",
    "country", "city", "services", "icp_signals", "tech", "booking_url", "phone", "last_activity",
    "client_name", "client_role", "client_niche", "client_website", "client_evidence", "client_evidence_url", "clients_count",
    "funnel_url", "funnel_type", "funnel_platform", "funnel_offer", "funnel_price", "funnel_steps",
    "tagline", "description", "pages_crawled", "key_pages", "reject_reason",
]


def _best_client(a: Agency) -> Client | None:
    if not a.clients:
        return None
    return sorted(a.clients, key=lambda c: (c.status != "resolved", not c.funnels, -c.confidence))[0]


def agency_row(a: Agency) -> dict:
    primary = next((e for e in a.emails if e.is_primary), None) or (a.emails[0] if a.emails else None)
    client = _best_client(a)
    funnel = client.funnels[0] if client and client.funnels else None
    return {
        "tier": a.tier or "",
        "score": a.score,
        "status": a.status,
        "agency_name": a.name or "",
        "website": a.website,
        "domain": a.domain,
        "email": primary.email if primary else "",
        "email_verification": primary.verification if primary else "",
        "email_source": primary.source if primary else "",
        "all_emails": "; ".join(e.email for e in a.emails),
        "founder_name": a.founder_name or "",
        "founder_title": a.founder_title or "",
        "founder_linkedin": a.founder_linkedin or "",
        "founder_source": a.founder_source or "",
        "linkedin_company": (a.socials or {}).get("linkedin", ""),
        "instagram": (a.socials or {}).get("instagram", ""),
        "facebook": (a.socials or {}).get("facebook", ""),
        "youtube": (a.socials or {}).get("youtube", ""),
        "country": a.country or "",
        "city": a.city or "",
        "services": ", ".join(a.services or []),
        "icp_signals": ", ".join(a.icp_signals or []),
        "tech": ", ".join(a.tech or []),
        "booking_url": a.booking_url or "",
        "phone": (a.phones or [""])[0],
        "last_activity": a.last_activity or "",
        "client_name": client.name if client else "",
        "client_role": (client.role_title or "") if client else "",
        "client_niche": (client.niche or "") if client else "",
        "client_website": (client.website or "") if client else "",
        "client_evidence": (client.evidence or "")[:300] if client else "",
        "client_evidence_url": (client.evidence_url or "") if client else "",
        "clients_count": len(a.clients),
        "funnel_url": funnel.url if funnel else "",
        "funnel_type": funnel.funnel_type if funnel else "",
        "funnel_platform": (funnel.platform or "") if funnel else "",
        "funnel_offer": (funnel.offer or "") if funnel else "",
        "funnel_price": (funnel.price_hint or "") if funnel else "",
        "funnel_steps": " → ".join(s.get("step", "") for s in (funnel.steps or [])) if funnel else "",
        "tagline": a.tagline or "",
        "description": (a.description or "")[:500],
        "pages_crawled": a.pages_crawled,
        "key_pages": "; ".join(f"{k}={v}" for k, v in (a.key_pages or {}).items()),
        "reject_reason": a.reject_reason or "",
    }


async def export_csv(path: Path, *, statuses: tuple[str, ...] = ("qualified", "review"), tiers: tuple[str, ...] | None = None, min_score: int = 0) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as s:
        q = select(Agency).where(Agency.status.in_(statuses), Agency.score >= min_score).options(
            selectinload(Agency.emails), selectinload(Agency.clients).selectinload(Client.funnels)
        ).order_by(Agency.tier, Agency.score.desc())
        if tiers:
            q = q.where(Agency.tier.in_(tiers))
        agencies = list((await s.execute(q)).scalars())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for a in agencies:
            w.writerow(agency_row(a))
    return len(agencies)
