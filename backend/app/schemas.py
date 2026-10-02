"""Pydantic schemas exposed by the API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- runs -------------------------------------------------------------------------------------------
class RunCreate(BaseModel):
    name: str = "Acquisition agencies × infopreneurs"
    target_leads: int = Field(1000, ge=1, le=20000)
    engines: list[str] | None = None
    countries: list[str] | None = None
    max_queries: int | None = Field(None, description="Cap the query matrix (useful for a quick test)")
    stages: list[str] | None = Field(None, description="Subset of stages to run (default: all)")
    seed_domains: list[str] = Field(default_factory=list, description="Extra domains to inject as candidates")
    extra_queries: list[str] = Field(default_factory=list)
    min_score: int | None = None
    resume: bool = Field(True, description="Skip candidates already processed")


class RunOut(ORM):
    id: int
    name: str
    status: str
    stage: str
    config: dict[str, Any]
    stats: dict[str, Any]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class EventOut(ORM):
    id: int
    ts: datetime
    level: str
    stage: str
    message: str
    data: dict[str, Any] | None


# --- leads ------------------------------------------------------------------------------------------
class EmailOut(ORM):
    id: int
    email: str
    source: str
    page_url: str | None
    verification: str
    is_primary: bool
    confidence: float


class FunnelOut(ORM):
    id: int
    url: str
    funnel_type: str
    platform: str | None
    offer: str | None
    price_hint: str | None
    steps: list[dict[str, Any]]
    evidence: dict[str, Any]
    confidence: float


class ClientOut(ORM):
    id: int
    name: str
    kind: str
    role_title: str | None
    niche: str | None
    evidence: str | None
    evidence_url: str | None
    website: str | None
    website_source: str | None
    confidence: float
    status: str
    funnels: list[FunnelOut] = []


class LeadSummary(ORM):
    id: int
    domain: str
    website: str
    name: str | None
    tagline: str | None
    country: str | None
    city: str | None
    language: str | None
    services: list[str]
    icp_signals: list[str]
    tech: list[str]
    score: int
    status: str
    user_status: str | None
    alive: bool
    last_activity: str | None
    created_at: datetime
    primary_email: str | None = None
    email_verification: str | None = None
    email_count: int = 0
    client_count: int = 0
    top_client: str | None = None
    top_client_role: str | None = None
    top_client_website: str | None = None
    top_funnel_url: str | None = None
    top_funnel_type: str | None = None
    top_funnel_platform: str | None = None


class LeadDetail(LeadSummary):
    description: str | None
    founded_year: int | None
    team_size_hint: str | None
    socials: dict[str, str]
    phones: list[str]
    booking_url: str | None
    alive_details: dict[str, Any]
    pages_crawled: int
    key_pages: dict[str, str]
    score_breakdown: dict[str, Any]
    reject_reason: str | None
    notes: str | None
    emails: list[EmailOut] = []
    clients: list[ClientOut] = []


class LeadPatch(BaseModel):
    user_status: str | None = None
    notes: str | None = None
    status: str | None = None


class Page(BaseModel):
    items: list[LeadSummary]
    total: int
    page: int
    size: int


class Stats(BaseModel):
    candidates: int
    candidates_by_status: dict[str, int]
    agencies: int
    agencies_by_status: dict[str, int]
    qualified: int
    with_email: int
    with_verified_email: int
    with_client: int
    with_funnel: int
    avg_score: float
    engines: dict[str, Any]
    active_run: RunOut | None
