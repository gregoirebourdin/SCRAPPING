"""Extraction contracts (people, titles)."""

from __future__ import annotations

from dataclasses import dataclass, field

from scout.db.enums import RoleFamily, Seniority


@dataclass
class TitleInfo:
    original: str
    normalized_title: str  # canonical English label, e.g. "Chief Executive Officer"
    role_family: RoleFamily
    seniority: Seniority
    department: str | None
    decision_power: int  # 0–100


@dataclass
class PersonCandidate:
    full_name: str
    first_name: str | None
    last_name: str | None
    title: str | None
    source_url: str | None
    source_type: (
        str  # SourceType value: website / registry / ai_extraction / grounded_search / public_profile
    )
    method: str  # jsonld | team_card | legal_notice | text_pattern | registry | ai | grounded | mailto
    evidence: str  # verbatim snippet supporting the name (+ title)
    confidence: float  # identity confidence 0–1
    page_type: str | None = None
    email: str | None = None  # published email clearly tied to this person, if any
    profile_url: str | None = None
    extra: dict[str, str] = field(default_factory=dict)
