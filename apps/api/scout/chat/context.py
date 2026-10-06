"""UI context the chat operator automatically knows (spec §35)."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scout.db.enums import EntityType


class UIContext(BaseModel):
    model_config = ConfigDict(extra="ignore")
    list_id: uuid.UUID | None = None
    list_name: str | None = None
    view_id: uuid.UUID | None = None
    view_name: str | None = None
    scope: str = "people"  # list | people | companies | campaign
    entity_type: EntityType = EntityType.person
    selected_ids: list[uuid.UUID] = Field(default_factory=list)
    filters: dict[str, Any] | None = None
    sort: list[dict[str, Any]] = Field(default_factory=list)
    visible_columns: list[str] = Field(default_factory=list)
    row_count: int | None = None
    campaign_id: uuid.UUID | None = None
