"""SQLAlchemy models — the canonical schema (see docs/DATABASE.md). Migrated with Alembic."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from scout.db import enums as E
from scout.db.ids import uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def enum_col(enum_cls: type[StrEnum], name: str | None = None) -> sa.Enum:
    return sa.Enum(
        enum_cls,
        name=name or enum_cls.__name__.lower(),
        native_enum=False,
        create_constraint=True,
        length=48,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)


def ws_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )


def created_at() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def updated_at() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )


def tstz(nullable: bool = True) -> Mapped[datetime | None]:
    return mapped_column(sa.DateTime(timezone=True), nullable=nullable)


def fk_uuid(target: str, ondelete: str = "SET NULL", nullable: bool = True, index: bool = False) -> Any:
    return mapped_column(
        UUID(as_uuid=True), sa.ForeignKey(target, ondelete=ondelete), nullable=nullable, index=index
    )


class DiscoveryHistoryMixin:
    """Discovery-history columns shared by companies and people (spec §25)."""

    first_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    last_enriched_at: Mapped[datetime | None] = tstz()
    first_campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    times_discovered: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    times_exported: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    last_exported_at: Mapped[datetime | None] = tstz()
    times_added_to_lists: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    contacted_at: Mapped[datetime | None] = tstz()
    suppressed_at: Mapped[datetime | None] = tstz()


# =========================================================================================
# 1. Identity & tenancy (Better Auth tables use text ids)
# =========================================================================================


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    email: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    email_verified: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    image: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    token: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    ip_address: Mapped[str | None] = mapped_column(sa.Text)
    user_agent: Mapped[str | None] = mapped_column(sa.Text)
    user_id: Mapped[str] = mapped_column(
        sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )


class AuthAccount(Base):
    __tablename__ = "auth_accounts"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[str] = mapped_column(
        sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    access_token: Mapped[str | None] = mapped_column(sa.Text)
    refresh_token: Mapped[str | None] = mapped_column(sa.Text)
    id_token: Mapped[str | None] = mapped_column(sa.Text)
    access_token_expires_at: Mapped[datetime | None] = tstz()
    refresh_token_expires_at: Mapped[datetime | None] = tstz()
    scope: Mapped[str | None] = mapped_column(sa.Text)
    password: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class AuthVerification(Base):
    __tablename__ = "auth_verifications"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    identifier: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    value: Mapped[str] = mapped_column(sa.Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    monthly_budget_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(10, 2), nullable=False, server_default="30"
    )
    hard_budget_cap: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[E.MemberRole] = mapped_column(
        enum_col(E.MemberRole), nullable=False, server_default="member"
    )
    created_at: Mapped[datetime] = created_at()


# =========================================================================================
# 2. Organization
# =========================================================================================


class List(Base):
    __tablename__ = "lists"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    entity_type: Mapped[E.EntityType] = mapped_column(
        enum_col(E.EntityType, "list_entity_type"), nullable=False, server_default="person"
    )
    color: Mapped[str | None] = mapped_column(sa.Text)
    is_archived: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    archived_at: Mapped[datetime | None] = tstz()
    source_campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    filter_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[str | None] = mapped_column(sa.Text, sa.ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        sa.Index(
            "uq_lists_workspace_name_active",
            "workspace_id",
            sa.text("lower(name)"),
            unique=True,
            postgresql_where=sa.text("NOT is_archived"),
        ),
    )


class ListMembership(Base):
    __tablename__ = "list_memberships"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    list_id: Mapped[uuid.UUID] = fk_uuid("lists.id", ondelete="CASCADE", nullable=False)
    person_id: Mapped[uuid.UUID | None] = fk_uuid("people.id", ondelete="CASCADE")
    company_id: Mapped[uuid.UUID | None] = fk_uuid("companies.id", ondelete="CASCADE")
    added_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    added_by: Mapped[str | None] = mapped_column(sa.Text)
    added_via: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="manual")
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        sa.CheckConstraint("(person_id IS NULL) <> (company_id IS NULL)", name="exactly_one_entity"),
        sa.Index(
            "uq_list_memberships_person",
            "list_id",
            "person_id",
            unique=True,
            postgresql_where=sa.text("person_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_list_memberships_company",
            "list_id",
            "company_id",
            unique=True,
            postgresql_where=sa.text("company_id IS NOT NULL"),
        ),
        sa.Index("ix_list_memberships_list_cursor", "list_id", "added_at", "id"),
        sa.Index("ix_list_memberships_ws_person", "workspace_id", "person_id"),
        sa.Index("ix_list_memberships_ws_company", "workspace_id", "company_id"),
    )


class SavedView(Base):
    __tablename__ = "saved_views"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    list_id: Mapped[uuid.UUID | None] = fk_uuid("lists.id", ondelete="CASCADE", index=True)
    entity_type: Mapped[E.EntityType] = mapped_column(
        enum_col(E.EntityType, "view_entity_type"), nullable=False, server_default="person"
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    sort: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    column_order: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    column_visibility: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    column_widths: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    pinned_columns: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    density: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="compact")
    is_default: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


# =========================================================================================
# 3. Campaigns
# =========================================================================================


class CampaignTemplate(Base):
    __tablename__ = "campaign_templates"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    prompt: Mapped[str | None] = mapped_column(sa.Text)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enrichment_plan: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    last_run_at: Mapped[datetime | None] = tstz()
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prompt: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[E.CampaignStatus] = mapped_column(
        enum_col(E.CampaignStatus), nullable=False, server_default="draft"
    )
    stop_reason: Mapped[str | None] = mapped_column(sa.Text)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    definition_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    interpretation: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    target_qualified_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    target_list_id: Mapped[uuid.UUID | None] = fk_uuid("lists.id")
    mode: Mapped[E.CampaignMode] = mapped_column(
        enum_col(E.CampaignMode), nullable=False, server_default="people"
    )
    seed_type: Mapped[E.SeedType] = mapped_column(
        enum_col(E.SeedType), nullable=False, server_default="search"
    )
    seed_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    max_cost_usd: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 2))
    max_raw_candidates: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="60000")
    max_runtime_hours: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="72")
    template_id: Mapped[uuid.UUID | None] = fk_uuid("campaign_templates.id")
    parent_campaign_id: Mapped[uuid.UUID | None] = fk_uuid("campaigns.id")
    recurrence: Mapped[str | None] = mapped_column(sa.Text)  # reserved: weekly/monthly fresh runs
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    started_at: Mapped[datetime | None] = tstz()
    paused_at: Mapped[datetime | None] = tstz()
    stopped_at: Mapped[datetime | None] = tstz()
    last_progress_at: Mapped[datetime | None] = tstz()

    __table_args__ = (sa.Index("ix_campaigns_ws_status", "workspace_id", "status"),)


class CampaignFilter(Base):
    __tablename__ = "campaign_filters"
    id: Mapped[uuid.UUID] = pk()
    campaign_id: Mapped[uuid.UUID] = fk_uuid("campaigns.id", ondelete="CASCADE", nullable=False, index=True)
    scope: Mapped[str] = mapped_column(sa.Text, nullable=False)  # company|person|website|email|score
    field: Mapped[str] = mapped_column(sa.Text, nullable=False)
    operator: Mapped[str] = mapped_column(sa.Text, nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    condition_kind: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="exact")
    is_required: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")


class CampaignSource(Base):
    __tablename__ = "campaign_sources"
    id: Mapped[uuid.UUID] = pk()
    campaign_id: Mapped[uuid.UUID] = fk_uuid("campaigns.id", ondelete="CASCADE", nullable=False, index=True)
    source_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    status: Mapped[E.CampaignSourceStatus] = mapped_column(
        enum_col(E.CampaignSourceStatus), nullable=False, server_default="pending"
    )
    query_plan: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    raw_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    unique_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    qualified_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    error_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    last_run_at: Mapped[datetime | None] = tstz()
    last_error: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (sa.UniqueConstraint("campaign_id", "source_key"),)


class CampaignExclusion(Base):
    __tablename__ = "campaign_exclusions"
    id: Mapped[uuid.UUID] = pk()
    campaign_id: Mapped[uuid.UUID] = fk_uuid("campaigns.id", ondelete="CASCADE", nullable=False, index=True)
    mode: Mapped[E.ExclusionMode] = mapped_column(enum_col(E.ExclusionMode), nullable=False)
    entity: Mapped[E.EntityType] = mapped_column(enum_col(E.EntityType, "exclusion_entity"), nullable=False)
    exposure_types: Mapped[list[str] | None] = mapped_column(ARRAY(sa.Text))
    within_days: Mapped[int | None] = mapped_column(sa.Integer)
    list_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    campaign_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    import_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    include_list_history: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())


class CampaignStats(Base):
    __tablename__ = "campaign_stats"
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    raw_discovered: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    unique_new_companies: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    duplicates: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    excluded_previous: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    suppressed: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    reserved_elsewhere: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    companies_evaluated: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    companies_matched: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    people_found: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    emails_found: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    emails_safe: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    emails_accepted: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    qualified: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    rejected: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    errors: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    in_flight: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 5), nullable=False, server_default="0")
    updated_at: Mapped[datetime] = updated_at()


class CampaignReservation(Base):
    __tablename__ = "campaign_reservations"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    campaign_id: Mapped[uuid.UUID] = fk_uuid("campaigns.id", ondelete="CASCADE", nullable=False, index=True)
    entity_type: Mapped[E.EntityType] = mapped_column(
        enum_col(E.EntityType, "reservation_entity"), nullable=False
    )
    entity_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[E.ReservationStatus] = mapped_column(
        enum_col(E.ReservationStatus), nullable=False, server_default="reserved"
    )
    reserved_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    __table_args__ = (
        sa.Index(
            "uq_campaign_reservations_active",
            "workspace_id",
            "entity_type",
            "entity_key",
            unique=True,
            postgresql_where=sa.text("status = 'reserved'"),
        ),
    )


# =========================================================================================
# 4. Registry: companies
# =========================================================================================


class Company(DiscoveryHistoryMixin, Base):
    __tablename__ = "companies"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(sa.Text)
    normalized_domain: Mapped[str | None] = mapped_column(sa.Text)
    website_url: Mapped[str | None] = mapped_column(sa.Text)
    description: Mapped[str | None] = mapped_column(sa.Text)
    country: Mapped[str | None] = mapped_column(sa.String(2))
    region: Mapped[str | None] = mapped_column(sa.Text)
    city: Mapped[str | None] = mapped_column(sa.Text)
    postal_code: Mapped[str | None] = mapped_column(sa.Text)
    address: Mapped[str | None] = mapped_column(sa.Text)
    latitude: Mapped[float | None] = mapped_column(sa.Float)
    longitude: Mapped[float | None] = mapped_column(sa.Float)
    industry: Mapped[str | None] = mapped_column(sa.Text)
    sub_industry: Mapped[str | None] = mapped_column(sa.Text)
    category_raw: Mapped[str | None] = mapped_column(sa.Text)
    employee_min: Mapped[int | None] = mapped_column(sa.Integer)
    employee_max: Mapped[int | None] = mapped_column(sa.Integer)
    employee_confidence: Mapped[float | None] = mapped_column(sa.Float)
    phone: Mapped[str | None] = mapped_column(sa.Text)
    linkedin_url: Mapped[str | None] = mapped_column(sa.Text)
    registry_source: Mapped[str | None] = mapped_column(sa.Text)
    registry_id: Mapped[str | None] = mapped_column(sa.Text)
    founded_year: Mapped[int | None] = mapped_column(sa.Integer)
    status: Mapped[E.CompanyStatus] = mapped_column(
        enum_col(E.CompanyStatus), nullable=False, server_default="active"
    )
    website_status: Mapped[E.WebsiteStatus] = mapped_column(
        enum_col(E.WebsiteStatus), nullable=False, server_default="unknown"
    )
    company_confidence: Mapped[float | None] = mapped_column(sa.Float)
    has_conflicts: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    needs_review: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    last_crawled_at: Mapped[datetime | None] = tstz()
    # Last technology fingerprint scan (also when nothing was detected: zero-tech sites are not re-scanned
    # before the freshness window ends).
    last_tech_scan_at: Mapped[datetime | None] = tstz()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        sa.Index(
            "uq_companies_ws_domain",
            "workspace_id",
            "normalized_domain",
            unique=True,
            postgresql_where=sa.text("normalized_domain IS NOT NULL"),
        ),
        sa.Index(
            "uq_companies_ws_registry",
            "workspace_id",
            "registry_source",
            "registry_id",
            unique=True,
            postgresql_where=sa.text("registry_id IS NOT NULL"),
        ),
        sa.Index(
            "ix_companies_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ),
        sa.Index("ix_companies_ws_country_industry", "workspace_id", "country", "industry"),
        sa.Index("ix_companies_ws_size", "workspace_id", "employee_min", "employee_max"),
    )


class CompanyFieldObservation(Base):
    __tablename__ = "company_field_observations"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    company_id: Mapped[uuid.UUID] = fk_uuid("companies.id", ondelete="CASCADE", nullable=False)
    field_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    value_json: Mapped[Any] = mapped_column(JSONB, nullable=True)
    source_type: Mapped[E.SourceType] = mapped_column(
        enum_col(E.SourceType, "company_obs_source"), nullable=False
    )
    source_key: Mapped[str | None] = mapped_column(sa.Text)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    source_title: Mapped[str | None] = mapped_column(sa.Text)
    page_id: Mapped[uuid.UUID | None] = fk_uuid("website_pages.id")
    evidence: Mapped[str | None] = mapped_column(sa.Text)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False, server_default="0.5")
    is_user_confirmed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    observed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (sa.Index("ix_company_obs_company_field", "company_id", "field_name"),)


class CompanyDiscoveryEvent(Base):
    __tablename__ = "company_discovery_events"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    campaign_id: Mapped[uuid.UUID] = fk_uuid("campaigns.id", ondelete="CASCADE", nullable=False)
    company_id: Mapped[uuid.UUID | None] = fk_uuid("companies.id", ondelete="SET NULL", index=True)
    source_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_entity_id: Mapped[str | None] = mapped_column(sa.Text)
    candidate_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name: Mapped[str | None] = mapped_column(sa.Text)
    website: Mapped[str | None] = mapped_column(sa.Text)
    domain: Mapped[str | None] = mapped_column(sa.Text)
    location: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    category: Mapped[str | None] = mapped_column(sa.Text)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    stage: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="discovered")
    stage_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    outcome: Mapped[E.CandidateOutcome] = mapped_column(
        enum_col(E.CandidateOutcome), nullable=False, server_default="pending"
    )
    reason: Mapped[str | None] = mapped_column(sa.Text)
    observed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = tstz()

    __table_args__ = (
        sa.UniqueConstraint("campaign_id", "candidate_key"),
        sa.Index("ix_company_events_campaign_outcome", "campaign_id", "outcome"),
    )


# =========================================================================================
# 4b. Registry: people
# =========================================================================================


class Person(DiscoveryHistoryMixin, Base):
    __tablename__ = "people"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    company_id: Mapped[uuid.UUID | None] = fk_uuid("companies.id", ondelete="SET NULL", index=True)
    first_name: Mapped[str | None] = mapped_column(sa.Text)
    last_name: Mapped[str | None] = mapped_column(sa.Text)
    full_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    job_title: Mapped[str | None] = mapped_column(sa.Text)
    normalized_title: Mapped[str | None] = mapped_column(sa.Text)
    department: Mapped[str | None] = mapped_column(sa.Text)
    seniority: Mapped[E.Seniority | None] = mapped_column(enum_col(E.Seniority))
    role_family: Mapped[E.RoleFamily | None] = mapped_column(enum_col(E.RoleFamily))
    decision_power: Mapped[int | None] = mapped_column(sa.Integer)
    public_profile_url: Mapped[str | None] = mapped_column(sa.Text)
    location: Mapped[str | None] = mapped_column(sa.Text)
    phone: Mapped[str | None] = mapped_column(sa.Text)
    identity_confidence: Mapped[float | None] = mapped_column(sa.Float)
    primary_email_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    needs_review: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    has_conflicts: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    last_verified_at: Mapped[datetime | None] = tstz()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        sa.Index(
            "uq_people_ws_company_name",
            "workspace_id",
            "company_id",
            "normalized_name",
            unique=True,
            postgresql_where=sa.text("company_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_people_ws_profile",
            "workspace_id",
            "public_profile_url",
            unique=True,
            postgresql_where=sa.text("public_profile_url IS NOT NULL"),
        ),
        sa.Index(
            "ix_people_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ),
    )


class PersonEmployment(Base):
    __tablename__ = "person_employments"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    person_id: Mapped[uuid.UUID] = fk_uuid("people.id", ondelete="CASCADE", nullable=False, index=True)
    company_id: Mapped[uuid.UUID] = fk_uuid("companies.id", ondelete="CASCADE", nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(sa.Text)
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    started_at: Mapped[datetime | None] = tstz()
    ended_at: Mapped[datetime | None] = tstz()
    source_key: Mapped[str | None] = mapped_column(sa.Text)
    observed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (sa.UniqueConstraint("person_id", "company_id"),)


class PersonFieldObservation(Base):
    __tablename__ = "person_field_observations"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    person_id: Mapped[uuid.UUID] = fk_uuid("people.id", ondelete="CASCADE", nullable=False)
    field_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    value_json: Mapped[Any] = mapped_column(JSONB, nullable=True)
    source_type: Mapped[E.SourceType] = mapped_column(
        enum_col(E.SourceType, "person_obs_source"), nullable=False
    )
    source_key: Mapped[str | None] = mapped_column(sa.Text)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    source_title: Mapped[str | None] = mapped_column(sa.Text)
    page_id: Mapped[uuid.UUID | None] = fk_uuid("website_pages.id")
    evidence: Mapped[str | None] = mapped_column(sa.Text)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False, server_default="0.5")
    is_user_confirmed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    observed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (sa.Index("ix_person_obs_person_field", "person_id", "field_name"),)


class PersonDiscoveryEvent(Base):
    __tablename__ = "person_discovery_events"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    campaign_id: Mapped[uuid.UUID] = fk_uuid("campaigns.id", ondelete="CASCADE", nullable=False, index=True)
    person_id: Mapped[uuid.UUID | None] = fk_uuid("people.id", ondelete="SET NULL", index=True)
    company_id: Mapped[uuid.UUID | None] = fk_uuid("companies.id", ondelete="SET NULL")
    source_key: Mapped[str | None] = mapped_column(sa.Text)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    outcome: Mapped[E.CandidateOutcome] = mapped_column(
        enum_col(E.CandidateOutcome, "person_candidate_outcome"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(sa.Text)
    observed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class LeadExposure(Base):
    __tablename__ = "lead_exposures"
    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[E.EntityType] = mapped_column(
        enum_col(E.EntityType, "exposure_entity"), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    exposure_type: Mapped[E.ExposureType] = mapped_column(enum_col(E.ExposureType), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    list_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    import_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    export_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.Index(
            "ix_lead_exposures_lookup",
            "workspace_id",
            "entity_type",
            "entity_id",
            "exposure_type",
            "occurred_at",
        ),
        sa.Index("ix_lead_exposures_list", "workspace_id", "list_id"),
        sa.Index("ix_lead_exposures_campaign", "campaign_id"),
        sa.Index("ix_lead_exposures_import", "import_id"),
    )


class SuppressionEntry(Base):
    __tablename__ = "suppression_list"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    entity_type: Mapped[E.SuppressionEntity] = mapped_column(enum_col(E.SuppressionEntity), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    value: Mapped[str] = mapped_column(sa.Text, nullable=False)
    reason: Mapped[E.SuppressionReason] = mapped_column(enum_col(E.SuppressionReason), nullable=False)
    note: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (sa.UniqueConstraint("workspace_id", "entity_type", "value"),)


# =========================================================================================
# 5. Contact data
# =========================================================================================


class Email(Base):
    __tablename__ = "emails"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    person_id: Mapped[uuid.UUID | None] = fk_uuid("people.id", ondelete="CASCADE", index=True)
    company_id: Mapped[uuid.UUID | None] = fk_uuid("companies.id", ondelete="CASCADE", index=True)
    address: Mapped[str] = mapped_column(sa.Text, nullable=False)
    local_part: Mapped[str] = mapped_column(sa.Text, nullable=False)
    domain: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[E.EmailKind] = mapped_column(enum_col(E.EmailKind), nullable=False, server_default="person")
    discovery_method: Mapped[E.EmailDiscoveryMethod] = mapped_column(
        enum_col(E.EmailDiscoveryMethod), nullable=False
    )
    pattern: Mapped[str | None] = mapped_column(sa.Text)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[E.EmailStatus] = mapped_column(
        enum_col(E.EmailStatus), nullable=False, server_default="UNKNOWN"
    )
    mx_valid: Mapped[bool | None] = mapped_column(sa.Boolean)
    smtp_result: Mapped[E.SmtpResult] = mapped_column(
        enum_col(E.SmtpResult), nullable=False, server_default="not_attempted"
    )
    catch_all: Mapped[bool | None] = mapped_column(sa.Boolean)
    disposable: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    role_address: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    free_provider: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    pattern_confidence: Mapped[float | None] = mapped_column(sa.Float)
    overall_confidence: Mapped[float | None] = mapped_column(sa.Float)
    is_primary: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    is_user_confirmed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    last_checked_at: Mapped[datetime | None] = tstz()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "address"),
        sa.Index("ix_emails_ws_status", "workspace_id", "status"),
        sa.Index("ix_emails_domain", "domain"),
        # Global (cross-workspace) lookups by address: pattern-memory idempotence in scout.email.store.
        sa.Index("ix_emails_address", "address"),
    )


class EmailCheck(Base):
    __tablename__ = "email_checks"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    email_id: Mapped[uuid.UUID] = fk_uuid("emails.id", ondelete="CASCADE", nullable=False, index=True)
    verifier: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[E.EmailStatus] = mapped_column(
        enum_col(E.EmailStatus, "email_check_status"), nullable=False
    )
    mx_valid: Mapped[bool | None] = mapped_column(sa.Boolean)
    smtp_result: Mapped[E.SmtpResult] = mapped_column(
        enum_col(E.SmtpResult, "email_check_smtp"), nullable=False
    )
    catch_all: Mapped[bool | None] = mapped_column(sa.Boolean)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    error: Mapped[str | None] = mapped_column(sa.Text)
    checked_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class DomainEmailPattern(Base):
    __tablename__ = "domain_email_patterns"
    id: Mapped[uuid.UUID] = pk()
    domain: Mapped[str] = mapped_column(sa.Text, nullable=False)
    pattern: Mapped[str] = mapped_column(sa.Text, nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False, server_default="0")
    supporting_samples: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    successful_checks: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    failed_checks: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    last_verified_at: Mapped[datetime | None] = tstz()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (sa.UniqueConstraint("domain", "pattern"),)


class DomainDnsCache(Base):
    __tablename__ = "domain_dns_cache"
    domain: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    has_mx: Mapped[bool | None] = mapped_column(sa.Boolean)
    mx_hosts: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    has_a: Mapped[bool | None] = mapped_column(sa.Boolean)
    # RFC 7505 null MX ("0 ."): the domain explicitly accepts no mail.
    null_mx: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    catch_all: Mapped[bool | None] = mapped_column(sa.Boolean)
    catch_all_checked_at: Mapped[datetime | None] = tstz()
    checked_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    error: Mapped[str | None] = mapped_column(sa.Text)


# =========================================================================================
# 6. Website cache
# =========================================================================================


class WebsiteCrawlRun(Base):
    __tablename__ = "website_crawl_runs"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    company_id: Mapped[uuid.UUID] = fk_uuid("companies.id", ondelete="CASCADE", nullable=False, index=True)
    domain: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="running")
    tier_max: Mapped[E.FetchTier] = mapped_column(
        enum_col(E.FetchTier), nullable=False, server_default="http"
    )
    pages_fetched: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    pages_failed: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    pages_unchanged: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    error_category: Mapped[E.ErrorCategory | None] = mapped_column(
        enum_col(E.ErrorCategory, "crawl_error_cat")
    )
    error: Mapped[str | None] = mapped_column(sa.Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = tstz()


class WebsitePage(Base):
    __tablename__ = "website_pages"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    company_id: Mapped[uuid.UUID] = fk_uuid("companies.id", ondelete="CASCADE", nullable=False, index=True)
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    page_type: Mapped[E.PageType] = mapped_column(
        enum_col(E.PageType), nullable=False, server_default="other"
    )
    title: Mapped[str | None] = mapped_column(sa.Text)
    meta_description: Mapped[str | None] = mapped_column(sa.Text)
    content_text: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(sa.Integer)
    fetched_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    etag: Mapped[str | None] = mapped_column(sa.Text)
    last_modified: Mapped[str | None] = mapped_column(sa.Text)
    content_type: Mapped[str | None] = mapped_column(sa.Text)
    language: Mapped[str | None] = mapped_column(sa.Text)
    fetch_tier: Mapped[E.FetchTier] = mapped_column(
        enum_col(E.FetchTier, "page_fetch_tier"), nullable=False, server_default="http"
    )
    head_html: Mapped[str | None] = mapped_column(sa.Text)
    response_headers: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    links: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    emails: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    phones: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    structured_data: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    headings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    word_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    crawl_run_id: Mapped[uuid.UUID | None] = fk_uuid("website_crawl_runs.id")

    __table_args__ = (sa.UniqueConstraint("company_id", "canonical_url"),)


# =========================================================================================
# 7. Dynamic enrichment
# =========================================================================================


class CustomColumn(Base):
    __tablename__ = "custom_columns"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    list_id: Mapped[uuid.UUID | None] = fk_uuid("lists.id", ondelete="CASCADE", index=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False)
    data_type: Mapped[E.ColumnDataType] = mapped_column(enum_col(E.ColumnDataType), nullable=False)
    kind: Mapped[E.ColumnKind] = mapped_column(
        enum_col(E.ColumnKind), nullable=False, server_default="factual"
    )
    entity_type: Mapped[E.EntityType] = mapped_column(
        enum_col(E.EntityType, "column_entity_type"), nullable=False, server_default="company"
    )
    resolver_type: Mapped[E.ResolverType] = mapped_column(enum_col(E.ResolverType), nullable=False)
    instructions: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    source_preferences: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    confidence_threshold: Mapped[float] = mapped_column(sa.Float, nullable=False, server_default="0.8")
    refresh_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    depends_on: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    is_hidden: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        sa.Index(
            "uq_custom_columns_ws_list_slug",
            "workspace_id",
            "list_id",
            "slug",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )


class CustomFieldValue(Base):
    __tablename__ = "custom_field_values"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    column_id: Mapped[uuid.UUID] = fk_uuid("custom_columns.id", ondelete="CASCADE", nullable=False)
    entity_type: Mapped[E.EntityType] = mapped_column(
        enum_col(E.EntityType, "cell_entity_type"), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    value_json: Mapped[Any] = mapped_column(JSONB, nullable=True)
    display_value: Mapped[str | None] = mapped_column(sa.Text)
    confidence: Mapped[float | None] = mapped_column(sa.Float)
    source_id: Mapped[str | None] = mapped_column(sa.Text)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    evidence: Mapped[str | None] = mapped_column(sa.Text)
    resolver: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[E.CellStatus] = mapped_column(
        enum_col(E.CellStatus), nullable=False, server_default="not_started"
    )
    error: Mapped[str | None] = mapped_column(sa.Text)
    input_hash: Mapped[str | None] = mapped_column(sa.Text)
    is_user_override: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    model: Mapped[str | None] = mapped_column(sa.Text)
    cost_usd: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 6))
    observed_at: Mapped[datetime | None] = tstz()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        sa.UniqueConstraint("column_id", "entity_type", "entity_id"),
        sa.Index("ix_cfv_column_display", "column_id", "display_value"),
        sa.Index("ix_cfv_entity", "entity_type", "entity_id"),
        sa.Index("ix_cfv_column_status", "column_id", "status"),
    )


# =========================================================================================
# 8. Signals, technology, scores
# =========================================================================================


class Technology(Base):
    __tablename__ = "technologies"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    company_id: Mapped[uuid.UUID] = fk_uuid("companies.id", ondelete="CASCADE", nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    category: Mapped[str | None] = mapped_column(sa.Text)
    version: Mapped[str | None] = mapped_column(sa.Text)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False, server_default="1")
    detector: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    observed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (sa.UniqueConstraint("company_id", "name"),)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    company_id: Mapped[uuid.UUID] = fk_uuid("companies.id", ondelete="CASCADE", nullable=False, index=True)
    person_id: Mapped[uuid.UUID | None] = fk_uuid("people.id", ondelete="CASCADE")
    type: Mapped[E.SignalType] = mapped_column(enum_col(E.SignalType), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    source_type: Mapped[E.SourceType] = mapped_column(enum_col(E.SourceType, "signal_source"), nullable=False)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    evidence: Mapped[str | None] = mapped_column(sa.Text)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class QualificationScore(Base):
    __tablename__ = "qualification_scores"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    campaign_id: Mapped[uuid.UUID | None] = fk_uuid("campaigns.id", ondelete="CASCADE")
    company_id: Mapped[uuid.UUID] = fk_uuid("companies.id", ondelete="CASCADE", nullable=False, index=True)
    person_id: Mapped[uuid.UUID | None] = fk_uuid("people.id", ondelete="CASCADE", index=True)
    icp_score: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    company_fit: Mapped[float | None] = mapped_column(sa.Float)
    person_fit: Mapped[float | None] = mapped_column(sa.Float)
    intent: Mapped[float | None] = mapped_column(sa.Float)
    contactability: Mapped[float | None] = mapped_column(sa.Float)
    evidence_score: Mapped[float | None] = mapped_column(sa.Float)
    company_confidence: Mapped[float | None] = mapped_column(sa.Float)
    person_confidence: Mapped[float | None] = mapped_column(sa.Float)
    email_confidence: Mapped[float | None] = mapped_column(sa.Float)
    enrichment_confidence: Mapped[float | None] = mapped_column(sa.Float)
    overall_confidence: Mapped[float | None] = mapped_column(sa.Float)
    qualified: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    gate_results: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    weights: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    explanation: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    computed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.Index(
            "uq_qualification_scores_key",
            "campaign_id",
            "company_id",
            "person_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )


# =========================================================================================
# 9. Sources (global)
# =========================================================================================


class Source(Base):
    __tablename__ = "sources"
    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)  # discovery|evidence|both
    description: Mapped[str | None] = mapped_column(sa.Text)
    quality_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    requests: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    successes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    failures: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    blocks: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    results: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    duplicates: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    qualified: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    total_latency_ms: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    consecutive_failures: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    unhealthy_until: Mapped[datetime | None] = tstz()
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    last_success_at: Mapped[datetime | None] = tstz()
    updated_at: Mapped[datetime] = updated_at()


# =========================================================================================
# 10. Jobs & events
# =========================================================================================


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    campaign_id: Mapped[uuid.UUID | None] = fk_uuid("campaigns.id", ondelete="CASCADE", index=True)
    type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[E.JobStatus] = mapped_column(
        enum_col(E.JobStatus), nullable=False, server_default="pending"
    )
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    dedupe_key: Mapped[str | None] = mapped_column(sa.Text)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="5")
    run_after: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    lease_expires_at: Mapped[datetime | None] = tstz()
    locked_by: Mapped[str | None] = mapped_column(sa.Text)
    heartbeat_at: Mapped[datetime | None] = tstz()
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    error_category: Mapped[E.ErrorCategory | None] = mapped_column(enum_col(E.ErrorCategory, "job_error_cat"))
    parent_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    blocked_by_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    started_at: Mapped[datetime | None] = tstz()
    finished_at: Mapped[datetime | None] = tstz()

    __table_args__ = (
        sa.Index(
            "ix_jobs_claim",
            sa.text("priority DESC"),
            "run_after",
            postgresql_where=sa.text("status IN ('pending','retrying') AND blocked_by_count = 0"),
        ),
        sa.Index(
            "ix_jobs_lease",
            "lease_expires_at",
            postgresql_where=sa.text("status IN ('claimed','running')"),
        ),
        sa.Index(
            "uq_jobs_dedupe_active",
            "workspace_id",
            "dedupe_key",
            unique=True,
            postgresql_where=sa.text(
                "dedupe_key IS NOT NULL AND status NOT IN ('completed','failed','cancelled','dead_letter')"
            ),
        ),
        sa.Index("ix_jobs_ws_status_type", "workspace_id", "status", "type"),
    )


class JobDependency(Base):
    __tablename__ = "job_dependencies"
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    id: Mapped[uuid.UUID] = pk()
    job_id: Mapped[uuid.UUID] = fk_uuid("jobs.id", ondelete="CASCADE", nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="running")
    error: Mapped[str | None] = mapped_column(sa.Text)
    error_category: Mapped[E.ErrorCategory | None] = mapped_column(
        enum_col(E.ErrorCategory, "attempt_error_cat")
    )
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = tstz()
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        sa.Index("ix_job_events_ws_id", "workspace_id", "id"),
        sa.Index("ix_job_events_campaign", "campaign_id", "id"),
    )


# =========================================================================================
# 11. Chat, audit, usage, IO
# =========================================================================================


class ChatThread(Base):
    __tablename__ = "chat_threads"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    list_id: Mapped[uuid.UUID | None] = fk_uuid("lists.id", ondelete="SET NULL", index=True)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="New conversation")
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    thread_id: Mapped[uuid.UUID] = fk_uuid("chat_threads.id", ondelete="CASCADE", nullable=False, index=True)
    role: Mapped[str] = mapped_column(sa.Text, nullable=False)  # user|assistant
    content: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    parts: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    model: Mapped[str | None] = mapped_column(sa.Text)
    tokens_in: Mapped[int | None] = mapped_column(sa.Integer)
    tokens_out: Mapped[int | None] = mapped_column(sa.Integer)
    created_at: Mapped[datetime] = created_at()


class AssistantAction(Base):
    __tablename__ = "assistant_actions"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    thread_id: Mapped[uuid.UUID | None] = fk_uuid("chat_threads.id", ondelete="CASCADE", index=True)
    message_id: Mapped[uuid.UUID | None] = fk_uuid("chat_messages.id", ondelete="SET NULL")
    tool_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[E.ActionStatus] = mapped_column(enum_col(E.ActionStatus), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(sa.Text)
    requires_confirmation: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    undo_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()
    confirmed_at: Mapped[datetime | None] = tstz()
    executed_at: Mapped[datetime | None] = tstz()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    actor_type: Mapped[E.ActorType] = mapped_column(enum_col(E.ActorType), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(sa.Text)
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(sa.Text)
    entity_ids: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False, server_default="{}")
    entity_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    assistant_action_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    summary: Mapped[str | None] = mapped_column(sa.Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    undo_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    undone_at: Mapped[datetime | None] = tstz()
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (sa.Index("ix_audit_logs_ws_id", "workspace_id", "id"),)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    category: Mapped[E.UsageCategory] = mapped_column(enum_col(E.UsageCategory), nullable=False)
    resolver: Mapped[str | None] = mapped_column(sa.Text)
    model: Mapped[str | None] = mapped_column(sa.Text)
    source_key: Mapped[str | None] = mapped_column(sa.Text)
    quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    tokens_in: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    tokens_out: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    estimated_cost_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False, server_default="0")
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        sa.Index("ix_usage_events_ws_created", "workspace_id", "created_at"),
        sa.Index("ix_usage_events_campaign", "campaign_id"),
    )


class Import(Base):
    __tablename__ = "imports"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    list_id: Mapped[uuid.UUID | None] = fk_uuid("lists.id", ondelete="SET NULL")
    filename: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[E.ImportStatus] = mapped_column(
        enum_col(E.ImportStatus), nullable=False, server_default="pending"
    )
    column_mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    mark_as_known: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    row_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    imported_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    merged_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    skipped_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    error_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    errors: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()
    finished_at: Mapped[datetime | None] = tstz()


class Export(Base):
    __tablename__ = "exports"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    list_id: Mapped[uuid.UUID | None] = fk_uuid("lists.id", ondelete="SET NULL")
    view_id: Mapped[uuid.UUID | None] = fk_uuid("saved_views.id", ondelete="SET NULL")
    scope: Mapped[E.ExportScope] = mapped_column(enum_col(E.ExportScope), nullable=False)
    columns_mode: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="visible")
    format: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="csv")
    row_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()


class Webhook(Base):
    __tablename__ = "webhooks"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    secret: Mapped[str] = mapped_column(sa.Text, nullable=False)
    events: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False, server_default="{}")
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    last_status: Mapped[int | None] = mapped_column(sa.Integer)
    last_delivery_at: Mapped[datetime | None] = tstz()
    created_at: Mapped[datetime] = created_at()


class WebsiteConditionResult(Base):
    """Cache of website-condition evaluations keyed by condition + content hash (spec §57)."""

    __tablename__ = "website_condition_results"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    company_id: Mapped[uuid.UUID] = fk_uuid("companies.id", ondelete="CASCADE", nullable=False)
    condition_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    passed: Mapped[bool | None] = mapped_column(sa.Boolean)  # None = unknown
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    evidence: Mapped[str | None] = mapped_column(sa.Text)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    resolver: Mapped[str] = mapped_column(sa.Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (sa.UniqueConstraint("company_id", "condition_hash", "content_hash"),)


class GroundedResearch(Base):
    """Persisted grounded web research: query, sources, result, evidence (spec §180)."""

    __tablename__ = "grounded_research"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    company_id: Mapped[uuid.UUID | None] = fk_uuid("companies.id", ondelete="CASCADE", index=True)
    person_id: Mapped[uuid.UUID | None] = fk_uuid("people.id", ondelete="CASCADE")
    purpose: Mapped[str] = mapped_column(sa.Text, nullable=False)
    query: Mapped[str] = mapped_column(sa.Text, nullable=False)
    search_queries: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    sources: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    result: Mapped[Any] = mapped_column(JSONB, nullable=True)
    selected_evidence: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    model: Mapped[str | None] = mapped_column(sa.Text)
    cache_key: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    created_at: Mapped[datetime] = created_at()

    # Cache lookup: latest research for (workspace, cache_key) within the freshness window.
    __table_args__ = (
        sa.Index("ix_grounded_research_ws_key_created", "workspace_id", "cache_key", "created_at"),
    )
