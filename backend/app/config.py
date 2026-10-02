"""Runtime configuration.

Every knob can be overridden with an environment variable prefixed ``LF_`` (e.g. ``LF_FETCH_CONCURRENCY=30``)
or a ``.env`` file placed next to this package.  Nothing here requires a paid provider: search engines are
scraped directly, email verification uses DNS/SMTP, and the browser is a local Chromium.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LF_", env_file=BACKEND_DIR / ".env", extra="ignore")

    # --- storage -------------------------------------------------------------------------------------
    data_dir: Path = Field(default=BACKEND_DIR / "data", description="Where the SQLite DB, caches and exports live")
    db_filename: str = "leadforge.db"

    # --- http fetching --------------------------------------------------------------------------------
    fetch_concurrency: int = Field(24, description="Global number of in-flight HTTP requests")
    per_host_concurrency: int = Field(2, description="In-flight requests allowed per host")
    per_host_min_delay: float = Field(0.8, description="Seconds between two requests to the same host")
    fetch_timeout: float = 20.0
    fetch_retries: int = 2
    max_html_bytes: int = 3_000_000
    page_cache_ttl_hours: int = Field(24 * 7, description="How long a fetched page is reused from cache")
    respect_robots: bool = Field(False, description="Honor robots.txt disallow rules for site crawling")
    proxy_url: str | None = Field(None, description="Optional proxy for every outbound request (http://user:pass@host:port)")

    # --- search engines -------------------------------------------------------------------------------
    engines: list[str] = Field(default_factory=lambda: ["google", "bing", "duckduckgo"])
    search_concurrency: int = Field(2, description="Parallel SERP requests (all engines combined)")
    search_min_delay: float = Field(2.5, description="Seconds between two requests to the same engine")
    search_pages_per_query: int = Field(2, description="SERP pages to walk per query when the engine paginates")
    search_countries: list[str] = Field(default_factory=lambda: ["us", "gb", "au", "ca"])
    serp_cache_ttl_hours: int = 24 * 3
    engine_cooldown_seconds: int = Field(900, description="Pause an engine after a captcha/challenge")
    engine_max_failures: int = Field(4, description="Consecutive failures before an engine is disabled for the run")

    # --- browser --------------------------------------------------------------------------------------
    browser_enabled: bool = True
    browser_executable: str | None = Field(None, description="Path to a Chromium binary (optional)")
    browser_pages: int = Field(3, description="Concurrent browser tabs")
    browser_headless: bool = True

    # --- crawling -------------------------------------------------------------------------------------
    crawl_max_pages: int = Field(14, description="Max pages fetched per agency site")
    crawl_max_client_pages: int = Field(8, description="Max pages fetched per client (infopreneur) site")
    min_text_chars_for_static: int = Field(600, description="Below this, retry the page with the browser")

    # --- qualification --------------------------------------------------------------------------------
    min_lead_score: int = Field(55, description="Score threshold to mark a lead as qualified")
    require_named_client: bool = Field(True, description="A lead needs at least one coach/infopreneur client")
    english_min_confidence: float = 0.75

    # --- enrichment -----------------------------------------------------------------------------------
    smtp_verify: bool = Field(True, description="Try SMTP RCPT verification (auto-disabled if port 25 is blocked)")
    smtp_timeout: float = 8.0
    smtp_concurrency: int = 4
    smtp_helo_domain: str = "mail.example.com"
    smtp_from_address: str = "verify@example.com"
    guess_email_patterns: bool = Field(True, description="Propose hello@/info@/contact@ when nothing is found")
    client_resolve_max_search: int = Field(2, description="SERP lookups allowed per client to find their website")

    # --- orchestration --------------------------------------------------------------------------------
    worker_batch: int = 40
    default_target_leads: int = 1000

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    def ensure_dirs(self) -> None:
        (self.data_dir / "exports").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "seeds").mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
