"""Public API request/response models (exported to TypeScript through OpenAPI)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scout.db.enums import EntityType, ExportScope, SuppressionReason
from scout.query.filters import FilterGroup, SortSpec
from scout.schemas.campaign import CampaignDefinition
from scout.services.lists import RowRef


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- workspace ---------------------------------------------------------------------------------


class WorkspaceOut(APIModel):
    id: uuid.UUID
    name: str
    slug: str
    role: str
    monthly_budget_usd: float
    hard_budget_cap: bool
    settings: dict[str, Any] = Field(default_factory=dict)


class MeOut(APIModel):
    user_id: str
    email: str | None
    name: str | None
    workspaces: list[WorkspaceOut]
    current_workspace_id: uuid.UUID
    ai_provider: str
    ai_models: dict[str, str]
    features: dict[str, bool]


class WorkspaceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    monthly_budget_usd: float | None = Field(default=None, ge=0, le=100000)
    hard_budget_cap: bool | None = None
    settings: dict[str, Any] | None = None


# ---- lists -------------------------------------------------------------------------------------


class ListOut(APIModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    entity_type: str
    color: str | None = None
    is_archived: bool
    count: int = 0
    created_at: datetime
    updated_at: datetime
    source_campaign_id: uuid.UUID | None = None


class ListCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    entity_type: EntityType = EntityType.person
    description: str | None = None
    color: str | None = None
    from_filter: RowRef | None = None


class ListUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    color: str | None = None
    is_archived: bool | None = None


class MembershipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: RowRef


class MoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: RowRef
    to_list_id: uuid.UUID


class MembershipResult(APIModel):
    list_id: uuid.UUID
    affected: int
    already_present: int = 0
    skipped_suppressed: int = 0
    audit_id: int | None = None


# ---- rows --------------------------------------------------------------------------------------


class RowsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["list", "people", "companies", "campaign", "review"] = "people"
    list_id: uuid.UUID | None = None
    campaign_id: uuid.UUID | None = None
    entity_type: EntityType = EntityType.person
    filters: FilterGroup | None = None
    sort: list[SortSpec] = Field(default_factory=list)
    search: str | None = None
    cursor: str | None = None
    limit: int = Field(default=300, ge=1, le=1000)
    with_total: bool = True
    ids: list[uuid.UUID] | None = None


class ColumnOut(APIModel):
    id: uuid.UUID
    list_id: uuid.UUID | None
    name: str
    slug: str
    data_type: str
    kind: str
    entity_type: str
    resolver_type: str
    instructions: str
    configuration: dict[str, Any]
    confidence_threshold: float
    refresh_policy: dict[str, Any]
    position: int
    is_hidden: bool
    created_at: datetime


class RowsOut(APIModel):
    rows: list[dict[str, Any]]
    next_cursor: str | None
    total: int
    columns: list[ColumnOut]


class FieldMeta(APIModel):
    key: str
    label: str
    type: str
    enum_values: list[str] = Field(default_factory=list)
    sortable: bool = True
    filterable: bool = True
    custom_column_id: uuid.UUID | None = None


# ---- views -------------------------------------------------------------------------------------


class ViewOut(APIModel):
    id: uuid.UUID
    list_id: uuid.UUID | None
    entity_type: str
    name: str
    filters: dict[str, Any]
    sort: list[Any]
    column_order: list[Any]
    column_visibility: dict[str, Any]
    column_widths: dict[str, Any]
    pinned_columns: list[Any]
    density: str
    is_default: bool
    position: int


class ViewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    list_id: uuid.UUID | None = None
    entity_type: EntityType = EntityType.person
    filters: FilterGroup = Field(default_factory=FilterGroup)
    sort: list[SortSpec] = Field(default_factory=list)
    column_order: list[str] = Field(default_factory=list)
    column_visibility: dict[str, bool] = Field(default_factory=dict)
    column_widths: dict[str, int] = Field(default_factory=dict)
    pinned_columns: list[str] = Field(default_factory=list)
    density: Literal["compact", "comfortable"] = "compact"


class ViewUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    filters: FilterGroup | None = None
    sort: list[SortSpec] | None = None
    column_order: list[str] | None = None
    column_visibility: dict[str, bool] | None = None
    column_widths: dict[str, int] | None = None
    pinned_columns: list[str] | None = None
    density: Literal["compact", "comfortable"] | None = None


# ---- columns -----------------------------------------------------------------------------------


class ColumnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    instruction: str | None = None
    list_id: uuid.UUID | None = None
    data_type: str | None = None
    run: bool = True
    rows: RowRef | None = None


class ColumnUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    instruction: str | None = None
    data_type: str | None = None
    confidence_threshold: float | None = Field(default=None, ge=0, le=1)
    refresh_days: int | None = Field(default=None, ge=1, le=365)
    source_preferences: list[str] | None = None
    is_hidden: bool | None = None
    position: int | None = None


class EnrichRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: RowRef | None = None
    list_id: uuid.UUID | None = None
    only_missing: bool = True
    force: bool = False


class CellEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: EntityType
    entity_id: uuid.UUID
    value: Any


class FieldEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    value: Any


# ---- campaigns ---------------------------------------------------------------------------------


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=3, max_length=4000)
    list_id: uuid.UUID | None = None
    selected_ids: list[uuid.UUID] = Field(default_factory=list)


class ParseOut(APIModel):
    definition: CampaignDefinition
    interpretation: list[dict[str, str]]
    parser: str


class CampaignCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str | None = None
    definition: CampaignDefinition | None = None
    target_list_id: uuid.UUID | None = None
    start: bool = True
    list_id: uuid.UUID | None = None
    selected_ids: list[uuid.UUID] = Field(default_factory=list)


class TemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    campaign_id: uuid.UUID | None = None
    definition: CampaignDefinition | None = None


class TemplateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    only_new: bool = True
    target_count: int | None = Field(default=None, ge=1, le=100000)


# ---- export / import / suppression -------------------------------------------------------------


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: ExportScope = ExportScope.view
    entity_type: EntityType = EntityType.person
    list_id: uuid.UUID | None = None
    view_id: uuid.UUID | None = None
    ids: list[uuid.UUID] | None = None
    filters: FilterGroup | None = None
    sort: list[SortSpec] = Field(default_factory=list)
    search: str | None = None
    columns: list[str] | None = None
    columns_mode: Literal["visible", "all"] = "all"
    format: Literal["csv", "json"] = "csv"


class SuppressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: RowRef | None = None
    emails: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    reason: SuppressionReason = SuppressionReason.manual
    note: str | None = None


class ReviewApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: EntityType = EntityType.person
    ids: list[uuid.UUID]


class WebhookCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=500)
    events: list[str] = Field(default_factory=lambda: ["lead.qualified"])


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: RowRef
    what: Literal["company", "person", "email", "column"] = "company"
    column_id: uuid.UUID | None = None
