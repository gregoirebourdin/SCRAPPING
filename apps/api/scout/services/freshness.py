"""Freshness rules by field (spec §90, §134) and UI labels (spec §153)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_RULES_DAYS: dict[str, int] = {
    "website": 30,
    "company_description": 30,
    "role": 45,
    "email": 60,
    "mx": 30,
    "email_pattern": 180,
    "technology": 30,
    "grounded_research": 14,
    "company": 30,
}


def rules_for(workspace_settings: dict[str, Any] | None) -> dict[str, int]:
    rules = dict(DEFAULT_RULES_DAYS)
    overrides = (workspace_settings or {}).get("freshness") or {}
    for k, v in overrides.items():
        if k in rules and isinstance(v, int) and v > 0:
            rules[k] = v
    return rules


def is_stale(observed_at: datetime | None, days: int) -> bool:
    if observed_at is None:
        return True
    return datetime.now(UTC) - observed_at > timedelta(days=days)


def freshness_label(observed_at: datetime | None) -> str:
    """Fresh (< 7 d) · 30d · 90d · Stale."""
    if observed_at is None:
        return "unknown"
    age = datetime.now(UTC) - observed_at
    if age < timedelta(days=7):
        return "fresh"
    if age < timedelta(days=30):
        return "30d"
    if age < timedelta(days=90):
        return "90d"
    return "stale"
