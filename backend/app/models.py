"""ORM models.

Lifecycle of a lead:

    SERP / directory / listicle  ──►  Candidate (a domain we have *heard of*)
                                         │ crawl + qualify
                                         ▼
                                      Agency (a real, live, English acquisition agency)
                                         ├── AgencyEmail (found / verified / guessed)
                                         └── Client (a coach / infopreneur they work with)
                                                └── Funnel (what that client's funnel looks like)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.utcnow()


# ----------------------------------------------------------------------------------------------------
# Runs & events
# ----------------------------------------------------------------------------------------------------
class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="run")
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|running|completed|failed|cancelled
    stage: Mapped[str] = mapped_column(String(64), default="init")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    events: Mapped[list[Event]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    level: Mapped[str] = mapped_column(String(16), default="info")
    stage: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    run: Mapped[Run | None] = relationship(back_populates="events")


# ----------------------------------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------------------------------
class Candidate(Base):
    """A domain discovered somewhere.  One row per registrable domain."""

    __tablename__ = "candidates"
    __table_args__ = (Index("ix_candidates_status_run", "status", "run_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    url: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64), default="serp")  # serp:bing | listicle | directory:teachable | seed
    query: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)
    hits: Mapped[int] = mapped_column(Integer, default=1)  # how many SERP results pointed at this domain
    status: Mapped[str] = mapped_column(String(32), default="new")  # new|crawling|crawled|qualified|rejected|error
    reject_reason: Mapped[str | None] = mapped_column(String(255))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)

    agency: Mapped[Agency | None] = relationship(back_populates="candidate", uselist=False)


class SerpCache(Base):
    __tablename__ = "serp_cache"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)  # engine|country|page|query
    engine: Mapped[str] = mapped_column(String(32))
    query: Mapped[str] = mapped_column(Text)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class PageCache(Base):
    """Raw fetched pages, zlib-compressed.  Keyed by normalized URL."""

    __tablename__ = "page_cache"

    url: Mapped[str] = mapped_column(String(2048), primary_key=True)
    final_url: Mapped[str | None] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(128))
    body: Mapped[bytes | None] = mapped_column(LargeBinary)
    rendered: Mapped[bool] = mapped_column(Boolean, default=False)  # fetched through the browser
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ----------------------------------------------------------------------------------------------------
# Leads
# ----------------------------------------------------------------------------------------------------
class Agency(Base):
    __tablename__ = "agencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id", ondelete="SET NULL"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)

    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    website: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(String(255), index=True)
    tagline: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(64), index=True)
    city: Mapped[str | None] = mapped_column(String(128))
    language: Mapped[str | None] = mapped_column(String(8))
    language_confidence: Mapped[float | None] = mapped_column(Float)
    founded_year: Mapped[int | None] = mapped_column(Integer)
    team_size_hint: Mapped[str | None] = mapped_column(String(64))

    services: Mapped[list[str]] = mapped_column(JSON, default=list)  # ["meta ads", "funnels", ...]
    icp_signals: Mapped[list[str]] = mapped_column(JSON, default=list)  # ["coaches", "course creators", ...]
    tech: Mapped[list[str]] = mapped_column(JSON, default=list)
    socials: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    phones: Mapped[list[str]] = mapped_column(JSON, default=list)
    booking_url: Mapped[str | None] = mapped_column(Text)  # Calendly / call booking page

    alive: Mapped[bool] = mapped_column(Boolean, default=True)
    alive_details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_activity: Mapped[str | None] = mapped_column(String(32))  # ISO date of newest signal found
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0)
    key_pages: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)  # kind → url

    # founder (from the site, or resolved through search engines — LinkedIn itself is never scraped)
    founder_name: Mapped[str | None] = mapped_column(String(160))
    founder_title: Mapped[str | None] = mapped_column(String(160))
    founder_linkedin: Mapped[str | None] = mapped_column(Text)
    founder_source: Mapped[str | None] = mapped_column(String(32))  # site_link|site_name+serp|serp|none
    founder_confidence: Mapped[float | None] = mapped_column(Float)

    score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tier: Mapped[str | None] = mapped_column(String(4), index=True)  # A|B|C
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)  # qualified|review|rejected
    reject_reason: Mapped[str | None] = mapped_column(String(255))
    enrich_stage: Mapped[str] = mapped_column(String(32), default="none")  # none|clients|founder|emails|done
    notes: Mapped[str | None] = mapped_column(Text)
    user_status: Mapped[str | None] = mapped_column(String(32))  # user-set: new|contacted|replied|ignored

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    candidate: Mapped[Candidate | None] = relationship(back_populates="agency")
    emails: Mapped[list[AgencyEmail]] = relationship(
        back_populates="agency", cascade="all, delete-orphan", order_by="AgencyEmail.rank"
    )
    clients: Mapped[list[Client]] = relationship(
        back_populates="agency", cascade="all, delete-orphan", order_by="Client.confidence.desc()"
    )


class AgencyEmail(Base):
    __tablename__ = "agency_emails"
    __table_args__ = (UniqueConstraint("agency_id", "email", name="uq_agency_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(32))  # mailto|text|cloudflare|jsonld|obfuscated|guessed
    page_url: Mapped[str | None] = mapped_column(Text)
    verification: Mapped[str] = mapped_column(String(32), default="unknown")
    # unknown | mx_valid | smtp_valid | catch_all | invalid | no_mx
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)

    agency: Mapped[Agency] = relationship(back_populates="emails")


class Client(Base):
    """A coach / course creator / infopreneur the agency names as a client."""

    __tablename__ = "clients"
    __table_args__ = (UniqueConstraint("agency_id", "name", name="uq_agency_client"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16), default="person")  # person|brand
    role_title: Mapped[str | None] = mapped_column(String(255))  # "Business coach", "Course creator"
    niche: Mapped[str | None] = mapped_column(String(128))
    evidence: Mapped[str | None] = mapped_column(Text)  # the sentence(s) proving the relationship
    evidence_url: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    website_source: Mapped[str | None] = mapped_column(String(32))  # outbound_link|search|none
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(32), default="found")  # found|resolved|no_site|not_infopreneur
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    agency: Mapped[Agency] = relationship(back_populates="clients")
    funnels: Mapped[list[Funnel]] = relationship(
        back_populates="client", cascade="all, delete-orphan", order_by="Funnel.confidence.desc()"
    )


class Funnel(Base):
    __tablename__ = "funnels"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    funnel_type: Mapped[str] = mapped_column(String(64))  # webinar|vsl|application|lead_magnet|challenge|...
    platform: Mapped[str | None] = mapped_column(String(64))  # clickfunnels|kajabi|gohighlevel|...
    offer: Mapped[str | None] = mapped_column(Text)  # headline / offer name
    price_hint: Mapped[str | None] = mapped_column(String(64))
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    client: Mapped[Client] = relationship(back_populates="funnels")


__all__ = [
    "Base",
    "Run",
    "Event",
    "Candidate",
    "SerpCache",
    "PageCache",
    "Agency",
    "AgencyEmail",
    "Client",
    "Funnel",
    "func",
]
