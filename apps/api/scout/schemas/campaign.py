"""Typed campaign definition (spec §28) — the contract between the ICP parser, the planner and the UI."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scout.db.enums import CampaignMode, EmailStatus, ExclusionMode, ExposureType, RoleFamily, Seniority


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EmployeeRange(Strict):
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _order(self) -> EmployeeRange:
        if self.min is not None and self.max is not None and self.min > self.max:
            self.min, self.max = self.max, self.min
        return self


class CompanyFilters(Strict):
    industries: list[str] = Field(
        default_factory=list, description="Free-text industries, e.g. 'marketing agency'"
    )
    keywords: list[str] = Field(default_factory=list, description="Extra discovery keywords")
    countries: list[str] = Field(default_factory=list, description="ISO-3166 alpha-2 codes, e.g. FR")
    regions: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    employee_range: EmployeeRange | None = None
    exclude_keywords: list[str] = Field(default_factory=list)

    @field_validator("countries")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [c.strip().upper()[:2] for c in v if c and c.strip()]


class KeywordCondition(Strict):
    """Deterministic: website text contains any/all terms (case and accent insensitive)."""

    type: Literal["keyword_any", "keyword_all"] = "keyword_any"
    terms: list[str] = Field(min_length=1)
    label: str | None = None
    required: bool = True


class RegexCondition(Strict):
    type: Literal["regex"] = "regex"
    pattern: str
    label: str | None = None
    required: bool = True


class SemanticCondition(Strict):
    """AI with evidence: the company actually offers / does / is something."""

    type: Literal["semantic_service", "semantic_classification"] = "semantic_service"
    concept: str
    keywords: list[str] = Field(default_factory=list, description="Hints used to retrieve relevant passages")
    label: str | None = None
    required: bool = True
    min_confidence: float = Field(default=0.7, ge=0, le=1)


class TechnologyCondition(Strict):
    type: Literal["technology"] = "technology"
    technologies: list[str] = Field(min_length=1)
    match: Literal["any", "all"] = "any"
    label: str | None = None
    required: bool = True


WebsiteCondition = Annotated[
    KeywordCondition | RegexCondition | SemanticCondition | TechnologyCondition,
    Field(discriminator="type"),
]


class PeopleFilters(Strict):
    titles: list[str] = Field(default_factory=list)
    role_families: list[RoleFamily] = Field(default_factory=list)
    seniorities: list[Seniority] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    max_people_per_company: int = Field(default=1, ge=1, le=10)


class ExclusionRuleSpec(Strict):
    entity: Literal["person", "company"]
    exposure_types: list[ExposureType] | None = None  # None = any user-facing exposure
    within_days: int | None = Field(default=None, ge=1)
    list_ids: list[uuid.UUID] = Field(default_factory=list)
    campaign_ids: list[uuid.UUID] = Field(default_factory=list)
    import_ids: list[uuid.UUID] = Field(default_factory=list)
    include_list_history: bool = True


class ExclusionSpec(Strict):
    mode: ExclusionMode = ExclusionMode.NONE
    previous_people: bool = False
    previous_companies: bool = False
    allow_new_people_at_existing_companies: bool = True
    list_ids: list[uuid.UUID] = Field(default_factory=list)
    list_scope: Literal["people", "companies", "both"] = "people"
    import_ids: list[uuid.UUID] = Field(default_factory=list)
    cooldown_days: int | None = Field(default=None, ge=1)
    include_company_scope: bool = False
    rules: list[ExclusionRuleSpec] = Field(default_factory=list)


class ScoreWeights(Strict):
    company_fit: float = 35
    person_fit: float = 20
    intent: float = 20
    contactability: float = 15
    evidence: float = 10


class EnrichmentRequest(Strict):
    name: str
    instruction: str
    required_value: str | None = Field(
        default=None, description="If set, only leads whose value equals this qualify"
    )


class SourcePrefs(Strict):
    preferred: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)


class Limits(Strict):
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_raw_candidates: int = Field(default=60000, ge=10)
    max_runtime_hours: int = Field(default=72, ge=1)


class Seed(Strict):
    type: Literal["search", "list", "import", "selection", "domains"] = "search"
    list_id: uuid.UUID | None = None
    import_id: uuid.UUID | None = None
    company_ids: list[uuid.UUID] = Field(default_factory=list)
    person_ids: list[uuid.UUID] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


RequiredField = Literal["company", "website", "person", "professional_email", "phone"]


def _default_required_fields() -> list[RequiredField]:
    return ["company", "person", "professional_email"]


class CampaignDefinition(Strict):
    version: Literal[1] = 1
    name: str | None = None
    mode: CampaignMode = CampaignMode.people
    target_qualified_count: int = Field(default=100, ge=1, le=100_000)
    company_filters: CompanyFilters = Field(default_factory=CompanyFilters)
    website_conditions: list[WebsiteCondition] = Field(default_factory=list)
    people_filters: PeopleFilters = Field(default_factory=PeopleFilters)
    required_fields: list[RequiredField] = Field(default_factory=_default_required_fields)
    minimum_email_confidence: int = Field(default=80, ge=0, le=100)
    minimum_person_confidence: int = Field(default=80, ge=0, le=100)
    minimum_company_fit: int = Field(default=60, ge=0, le=100)
    minimum_icp_score: int = Field(default=75, ge=0, le=100)
    accepted_email_statuses: list[EmailStatus] = Field(default_factory=lambda: [EmailStatus.SAFE])
    exclusion: ExclusionSpec = Field(default_factory=ExclusionSpec)
    enrichments: list[EnrichmentRequest] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    score_weights: ScoreWeights = Field(default_factory=ScoreWeights)
    sources: SourcePrefs = Field(default_factory=SourcePrefs)
    limits: Limits = Field(default_factory=Limits)
    seed: Seed = Field(default_factory=Seed)

    @model_validator(mode="after")
    def _consistency(self) -> CampaignDefinition:
        if self.mode == CampaignMode.companies:
            self.required_fields = [
                f for f in self.required_fields if f not in ("person", "professional_email")
            ]
        return self

    @property
    def requires_person(self) -> bool:
        return self.mode == CampaignMode.people and "person" in self.required_fields

    @property
    def requires_email(self) -> bool:
        return self.mode == CampaignMode.people and "professional_email" in self.required_fields


class InterpretationItem(BaseModel):
    label: str
    value: str


def interpret(
    defn: CampaignDefinition, list_names: dict[uuid.UUID, str] | None = None
) -> list[InterpretationItem]:
    """Compact human-readable interpretation card (spec §127)."""
    list_names = list_names or {}
    items: list[InterpretationItem] = []
    noun = "leads" if defn.mode == CampaignMode.people else "companies"
    fresh = defn.exclusion.mode != ExclusionMode.NONE
    items.append(
        InterpretationItem(
            label="Target", value=f"{defn.target_qualified_count:,} {'new ' if fresh else ''}{noun}"
        )
    )
    cf = defn.company_filters
    if cf.industries:
        items.append(
            InterpretationItem(label="Companies", value=", ".join(i.capitalize() for i in cf.industries))
        )
    if cf.countries or cf.regions or cf.cities:
        loc = ", ".join([*cf.cities, *cf.regions, *[_country_name(c) for c in cf.countries]])
        items.append(InterpretationItem(label="Location", value=loc))
    if cf.employee_range and (cf.employee_range.min is not None or cf.employee_range.max is not None):
        lo = cf.employee_range.min if cf.employee_range.min is not None else 0
        hi = cf.employee_range.max
        items.append(
            InterpretationItem(
                label="Size", value=f"{lo}–{hi} employees" if hi is not None else f"{lo}+ employees"
            )
        )
    for c in defn.website_conditions:
        if isinstance(c, KeywordCondition):
            joiner = " or " if c.type == "keyword_any" else " and "
            items.append(InterpretationItem(label="Website", value=f"Mentions {joiner.join(c.terms)}"))
        elif isinstance(c, SemanticCondition):
            items.append(InterpretationItem(label="Website", value=c.label or f"Actually: {c.concept}"))
        elif isinstance(c, TechnologyCondition):
            items.append(
                InterpretationItem(
                    label="Technology", value=(" or " if c.match == "any" else " and ").join(c.technologies)
                )
            )
        elif isinstance(c, RegexCondition):
            items.append(InterpretationItem(label="Website", value=c.label or f"Matches /{c.pattern}/"))
    if defn.mode == CampaignMode.people:
        pf = defn.people_filters
        roles = pf.titles or [r.value.replace("_", " ").title() for r in pf.role_families]
        if roles:
            items.append(InterpretationItem(label="People", value=" / ".join(roles)))
        items.append(
            InterpretationItem(
                label="Email",
                value="Required · "
                + "/".join(s.value.replace("_", "-") for s in defn.accepted_email_statuses)
                if defn.requires_email
                else "Optional",
            )
        )
    ex = defn.exclusion
    if ex.mode == ExclusionMode.NONE:
        items.append(InterpretationItem(label="Previously seen", value="Allowed (existing data reused)"))
    else:
        label = {
            ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE: "People excluded"
            + (
                " · new people at known companies allowed"
                if ex.allow_new_people_at_existing_companies
                else ""
            ),
            ExclusionMode.EXCLUDE_PREVIOUS_COMPANIES: "Companies excluded",
            ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE_AND_COMPANIES: "People and companies excluded",
            ExclusionMode.EXCLUDE_EXPORTED: "Previously exported excluded",
            ExclusionMode.EXCLUDE_SPECIFIC_LISTS: "Excluded lists: "
            + ", ".join(list_names.get(i, "list") for i in ex.list_ids),
            ExclusionMode.EXCLUDE_CURRENT_LIST: "Already in target list excluded",
            ExclusionMode.EXCLUDE_CONTACTED: "Contacted leads excluded",
            ExclusionMode.EXCLUDE_WITHIN_COOLDOWN: f"Seen in last {ex.cooldown_days or 90} days excluded",
            ExclusionMode.CUSTOM: "Custom exclusion rules",
        }[ex.mode]
        items.append(InterpretationItem(label="Previously seen", value=label))
    for e in defn.enrichments:
        items.append(InterpretationItem(label="Column", value=e.name))
    return items


_COUNTRIES = {
    "FR": "France",
    "BE": "Belgium",
    "CH": "Switzerland",
    "LU": "Luxembourg",
    "CA": "Canada",
    "US": "United States",
    "GB": "United Kingdom",
    "DE": "Germany",
    "ES": "Spain",
    "IT": "Italy",
    "NL": "Netherlands",
    "PT": "Portugal",
    "IE": "Ireland",
    "AT": "Austria",
    "SE": "Sweden",
    "DK": "Denmark",
    "NO": "Norway",
    "FI": "Finland",
    "PL": "Poland",
    "AU": "Australia",
    "MA": "Morocco",
    "TN": "Tunisia",
    "SN": "Senegal",
}


def _country_name(code: str) -> str:
    return _COUNTRIES.get(code.upper(), code.upper())
