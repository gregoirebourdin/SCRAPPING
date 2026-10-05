"""Domain enumerations. Stored as varchar + CHECK constraints, exported to TypeScript via OpenAPI."""

from __future__ import annotations

from enum import StrEnum


class MemberRole(StrEnum):
    owner = "owner"
    admin = "admin"
    member = "member"


class EntityType(StrEnum):
    person = "person"
    company = "company"


class SuppressionEntity(StrEnum):
    person = "person"
    company = "company"
    email = "email"
    domain = "domain"


class SuppressionReason(StrEnum):
    user_request = "user_request"
    opt_out = "opt_out"
    do_not_contact = "do_not_contact"
    manual = "manual"
    invalid = "invalid"
    gdpr = "gdpr"


class ExposureType(StrEnum):
    DISCOVERED = "DISCOVERED"
    SHOWN = "SHOWN"
    ADDED_TO_LIST = "ADDED_TO_LIST"
    IMPORTED = "IMPORTED"
    EXPORTED = "EXPORTED"
    CONTACTED = "CONTACTED"
    ENRICHED = "ENRICHED"


# Exposure types that mean "the user has already seen / owns this lead".
USER_FACING_EXPOSURES: tuple[ExposureType, ...] = (
    ExposureType.DISCOVERED,
    ExposureType.SHOWN,
    ExposureType.ADDED_TO_LIST,
    ExposureType.IMPORTED,
    ExposureType.EXPORTED,
    ExposureType.CONTACTED,
)


class ExclusionMode(StrEnum):
    NONE = "NONE"
    EXCLUDE_PREVIOUS_PEOPLE = "EXCLUDE_PREVIOUS_PEOPLE"
    EXCLUDE_PREVIOUS_COMPANIES = "EXCLUDE_PREVIOUS_COMPANIES"
    EXCLUDE_PREVIOUS_PEOPLE_AND_COMPANIES = "EXCLUDE_PREVIOUS_PEOPLE_AND_COMPANIES"
    EXCLUDE_EXPORTED = "EXCLUDE_EXPORTED"
    EXCLUDE_SPECIFIC_LISTS = "EXCLUDE_SPECIFIC_LISTS"
    EXCLUDE_CURRENT_LIST = "EXCLUDE_CURRENT_LIST"
    EXCLUDE_CONTACTED = "EXCLUDE_CONTACTED"
    EXCLUDE_WITHIN_COOLDOWN = "EXCLUDE_WITHIN_COOLDOWN"
    CUSTOM = "CUSTOM"


class CampaignStatus(StrEnum):
    draft = "draft"
    planning = "planning"
    running = "running"
    paused = "paused"
    completed = "completed"
    exhausted = "exhausted"
    budget_reached = "budget_reached"
    limit_reached = "limit_reached"
    cancelled = "cancelled"
    failed = "failed"


CAMPAIGN_ACTIVE = (CampaignStatus.planning, CampaignStatus.running)
CAMPAIGN_TERMINAL = (
    CampaignStatus.completed,
    CampaignStatus.exhausted,
    CampaignStatus.budget_reached,
    CampaignStatus.limit_reached,
    CampaignStatus.cancelled,
    CampaignStatus.failed,
)


class CampaignMode(StrEnum):
    people = "people"
    companies = "companies"


class SeedType(StrEnum):
    search = "search"
    list = "list"
    import_ = "import"
    selection = "selection"
    domains = "domains"


class CampaignSourceStatus(StrEnum):
    pending = "pending"
    active = "active"
    exhausted = "exhausted"
    unhealthy = "unhealthy"
    disabled = "disabled"


class ReservationStatus(StrEnum):
    reserved = "reserved"
    qualified = "qualified"
    released = "released"
    expired = "expired"


class CandidateOutcome(StrEnum):
    pending = "pending"
    qualified = "qualified"
    rejected = "rejected"
    duplicate = "duplicate"
    excluded_previous = "excluded_previous"
    suppressed = "suppressed"
    reserved_elsewhere = "reserved_elsewhere"
    error = "error"


class CompanyStatus(StrEnum):
    active = "active"
    closed = "closed"
    unknown = "unknown"


class WebsiteStatus(StrEnum):
    unknown = "unknown"
    ok = "ok"
    unreachable = "unreachable"
    parked = "parked"
    blocked = "blocked"
    none = "none"


class SourceType(StrEnum):
    website = "website"
    registry = "registry"
    maps = "maps"
    directory = "directory"
    search_snippet = "search_snippet"
    grounded_search = "grounded_search"
    ai_extraction = "ai_extraction"
    public_profile = "public_profile"
    import_ = "import"
    user = "user"
    tech_scan = "tech_scan"
    derived = "derived"


class EmailStatus(StrEnum):
    SAFE = "SAFE"
    RISKY = "RISKY"
    CATCH_ALL = "CATCH_ALL"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"


class EmailKind(StrEnum):
    person = "person"
    role = "role"
    generic = "generic"


class EmailDiscoveryMethod(StrEnum):
    published = "published"
    known_pattern = "known_pattern"
    inferred_pattern = "inferred_pattern"
    permutation = "permutation"
    import_ = "import"
    user = "user"


class SmtpResult(StrEnum):
    accepted = "accepted"
    rejected = "rejected"
    unknown = "unknown"
    timeout = "timeout"
    blocked = "blocked"
    not_attempted = "not_attempted"


class PageType(StrEnum):
    home = "home"
    about = "about"
    team = "team"
    services = "services"
    solutions = "solutions"
    contact = "contact"
    pricing = "pricing"
    careers = "careers"
    blog = "blog"
    news = "news"
    legal = "legal"
    case_studies = "case_studies"
    other = "other"


class FetchTier(StrEnum):
    http = "http"
    crawl4ai = "crawl4ai"
    browser = "browser"


class ColumnDataType(StrEnum):
    boolean = "boolean"
    text = "text"
    number = "number"
    url = "url"
    email = "email"
    enum = "enum"
    json = "json"
    date = "date"


class ColumnKind(StrEnum):
    factual = "factual"
    generated = "generated"


class ResolverType(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    CACHED_WEBSITE = "CACHED_WEBSITE"
    WEBSITE_RECRAWL = "WEBSITE_RECRAWL"
    TECH_DETECTION = "TECH_DETECTION"
    PUBLIC_SOURCE = "PUBLIC_SOURCE"
    WEB_SEARCH = "WEB_SEARCH"
    AI_ON_CACHED_CONTENT = "AI_ON_CACHED_CONTENT"
    AI_WEB_RESEARCH = "AI_WEB_RESEARCH"
    COMPOSITE = "COMPOSITE"


class CostClass(StrEnum):
    FREE = "FREE"
    CHEAP = "CHEAP"
    AI = "AI"
    WEB_SEARCH = "WEB_SEARCH"
    EXPENSIVE = "EXPENSIVE"


class CellStatus(StrEnum):
    not_started = "not_started"
    queued = "queued"
    running = "running"
    success = "success"
    unknown = "unknown"
    failed = "failed"
    stale = "stale"


class JobStatus(StrEnum):
    pending = "pending"
    claimed = "claimed"
    running = "running"
    completed = "completed"
    failed = "failed"
    retrying = "retrying"
    cancelled = "cancelled"
    paused = "paused"
    dead_letter = "dead_letter"


JOB_TERMINAL = (JobStatus.completed, JobStatus.failed, JobStatus.cancelled, JobStatus.dead_letter)


class ErrorCategory(StrEnum):
    network = "network"
    timeout = "timeout"
    blocked = "blocked"
    rate_limited = "rate_limited"
    not_found = "not_found"
    parse = "parse"
    ai = "ai"
    validation = "validation"
    budget = "budget"
    internal = "internal"


class SignalType(StrEnum):
    hiring = "hiring"
    job_opening = "job_opening"
    funding = "funding"
    product_launch = "product_launch"
    website_update = "website_update"
    new_executive = "new_executive"
    new_location = "new_location"
    press = "press"
    new_technology = "new_technology"
    expansion = "expansion"


class ActorType(StrEnum):
    user = "user"
    assistant = "assistant"
    system = "system"


class ActionStatus(StrEnum):
    running = "running"
    executed = "executed"
    failed = "failed"
    awaiting_confirmation = "awaiting_confirmation"
    rejected = "rejected"
    undone = "undone"


class UsageCategory(StrEnum):
    ai_tokens = "ai_tokens"
    grounded_search = "grounded_search"
    crawl_request = "crawl_request"
    browser_request = "browser_request"
    verification_request = "verification_request"
    maps_request = "maps_request"
    registry_request = "registry_request"
    web_search = "web_search"


class ImportStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class ExportScope(StrEnum):
    selected = "selected"
    view = "view"
    list = "list"


class Seniority(StrEnum):
    owner = "owner"
    c_level = "c_level"
    vp = "vp"
    director = "director"
    head = "head"
    manager = "manager"
    senior = "senior"
    entry = "entry"
    unknown = "unknown"


class RoleFamily(StrEnum):
    founder = "founder"
    executive = "executive"
    marketing = "marketing"
    sales = "sales"
    operations = "operations"
    technology = "technology"
    product = "product"
    finance = "finance"
    hr = "hr"
    creative = "creative"
    customer = "customer"
    legal = "legal"
    other = "other"
