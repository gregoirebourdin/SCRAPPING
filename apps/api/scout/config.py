"""Application settings (environment driven). Never hard-code secrets or model names elsewhere."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_json: bool = False

    # --- database -----------------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/scout"
    # Direct (non-pooled) URL: migrations + LISTEN/NOTIFY. Falls back to database_url.
    database_direct_url: str | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # --- auth between Next.js BFF and API ------------------------------------------------
    internal_api_secret: SecretStr = SecretStr("dev-internal-secret-change-me-32-bytes-minimum!!")
    internal_jwt_audience: str = "scout-api"
    internal_jwt_issuer: str = "scout-web"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- AI ------------------------------------------------------------------------------
    ai_provider: Literal["auto", "gemini", "local", "fake"] = "auto"
    gemini_api_key: SecretStr | None = None
    ai_model_fast: str = "gemini-3.1-flash-lite"
    ai_model_extractor: str = "gemini-3.1-flash-lite"
    ai_model_reasoning: str = "gemini-3.8-flash"
    ai_model_search: str = "gemini-3.8-flash"
    ai_timeout_seconds: float = 60.0

    # --- workers -------------------------------------------------------------------------
    worker_enabled: bool = True
    worker_slots: int = 16
    worker_lease_seconds: int = 120
    worker_poll_interval: float = 1.0
    worker_id: str | None = None

    # --- concurrency pools ---------------------------------------------------------------
    pool_http: int = 24
    pool_browser: int = 2
    pool_maps: int = 1
    pool_gemini: int = 6
    pool_search: int = 2
    pool_smtp: int = 4
    pool_public_api: int = 4
    per_domain_concurrency: int = 2
    per_domain_delay_ms: int = 400

    # --- crawler -------------------------------------------------------------------------
    crawler_user_agent: str = "ScoutBot/1.0 (+https://scout.example/bot; B2B research crawler)"
    crawler_respect_robots: bool = True
    crawler_max_pages: int = 10
    crawler_connect_timeout: float = 5.0
    crawler_read_timeout: float = 15.0
    crawler_max_bytes: int = 3_000_000
    crawler_enable_crawl4ai: bool = False
    crawler_enable_browser: bool = False
    # Test/dev only: host → "ip:port" overrides and private hosts allow-list (never in production).
    crawler_host_overrides: dict[str, str] = Field(default_factory=dict)
    crawler_allow_private_hosts: list[str] = Field(default_factory=list)

    # --- discovery sources -----------------------------------------------------------------
    gmaps_scraper_url: str | None = None  # e.g. http://maps-scraper.railway.internal:8080
    fr_registry_url: str = "https://recherche-entreprises.api.gouv.fr"
    ddg_html_url: str = "https://html.duckduckgo.com/html/"
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    yc_companies_url: str = "https://yc-oss.github.io/api/companies/all.json"
    hn_algolia_url: str = "https://hn.algolia.com/api/v1"
    github_api_url: str = "https://api.github.com"
    github_token: SecretStr | None = None
    discovery_fixture_manifest: str | None = None  # path to fixture manifest (tests/E2E)

    # --- email verification ---------------------------------------------------------------
    verifier_backend: Literal["auto", "service", "builtin", "fixture"] = "auto"
    verifier_service_url: str | None = None  # e.g. http://email-verifier.railway.internal:8080
    verifier_service_token: SecretStr | None = None
    smtp_enabled: bool = False
    smtp_helo_domain: str = "scout.example"
    smtp_from_address: str = "verify@scout.example"
    smtp_timeout: float = 10.0

    # --- tech detection --------------------------------------------------------------------
    tech_service_url: str | None = None  # same Go service as the verifier by default

    # --- costs ---------------------------------------------------------------------------
    default_monthly_budget_usd: float = 30.0
    cost_grounded_search_usd: float = 0.014
    cost_browser_request_usd: float = 0.0004
    cost_crawl_request_usd: float = 0.00002
    cost_verification_usd: float = 0.0

    # --- misc ----------------------------------------------------------------------------
    web_app_url: str = "http://localhost:3000"
    export_max_rows: int = 100_000

    @field_validator("cors_origins", "crawler_allow_private_hosts", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @property
    def direct_url(self) -> str:
        return self.database_direct_url or self.database_url

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def resolved_ai_provider(self) -> str:
        if self.ai_provider != "auto":
            return self.ai_provider
        return "gemini" if self.gemini_api_key and self.gemini_api_key.get_secret_value() else "local"

    def assert_safe(self) -> None:
        """Refuse dangerous test-only settings in production."""
        if self.is_production:
            if self.crawler_host_overrides or self.crawler_allow_private_hosts:
                raise RuntimeError("crawler overrides / private hosts are forbidden in production")
            if self.internal_api_secret.get_secret_value().startswith("dev-"):
                raise RuntimeError("INTERNAL_API_SECRET must be set in production")
            if self.verifier_backend == "fixture" or self.ai_provider == "fake":
                raise RuntimeError("test backends are forbidden in production")


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.assert_safe()
    return s
