"""Crawler contracts shared by the crawler, extractors, enrichment resolvers and the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from scout.db.enums import ErrorCategory, FetchTier, PageType, WebsiteStatus


class PageLike(Protocol):
    """Anything that looks like a cached website page (ORM `WebsitePage` rows or `FetchedPage`).

    Members are read-only properties so that both the ORM model (``Mapped[...]`` descriptors) and the
    crawler dataclass satisfy the protocol structurally; extractors never mutate pages. Every attribute
    an extractor reads must be declared here so mypy checks the seam between the crawl cache and the
    extractors.
    """

    @property
    def url(self) -> str: ...
    @property
    def page_type(self) -> PageType | str: ...
    @property
    def title(self) -> str | None: ...
    @property
    def meta_description(self) -> str | None: ...
    @property
    def content_text(self) -> str: ...
    @property
    def links(self) -> dict[str, Any]: ...
    @property
    def emails(self) -> list[Any]: ...
    @property
    def phones(self) -> list[Any]: ...
    @property
    def structured_data(self) -> list[Any]: ...
    @property
    def headings(self) -> list[Any]: ...


@dataclass
class FetchedPage:
    url: str  # requested URL
    final_url: str  # after redirects
    canonical_url: str  # scout.util.urls.canonical_url(final_url)
    status_code: int
    content_type: str | None
    page_type: PageType = PageType.other
    title: str | None = None
    meta_description: str | None = None
    content_text: str = ""  # cleaned visible text, ≤ 40 kB
    content_hash: str = ""  # scout.util.text.content_hash(content_text)
    language: str | None = None
    headers: dict[str, str] = field(default_factory=dict)  # lower-cased response headers
    head_html: str | None = None  # <head> + script/link tags (home page only), ≤ 48 kB
    links: dict[str, Any] = field(
        default_factory=dict
    )  # {"social": {"instagram": url, ...}, "internal": [{"url","text"}], "external": [...]}
    emails: list[str] = field(default_factory=list)  # lowercase addresses found (mailto + text)
    phones: list[str] = field(default_factory=list)
    structured_data: list[dict[str, Any]] = field(default_factory=list)  # JSON-LD objects
    headings: list[str] = field(default_factory=list)  # h1–h3 texts
    word_count: int = 0
    fetch_tier: FetchTier = FetchTier.http
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False  # 304 from a conditional request: reuse cached content


@dataclass
class CrawlResult:
    domain: str
    home_url: str | None
    status: WebsiteStatus  # ok / unreachable / parked / blocked
    pages: list[FetchedPage] = field(default_factory=list)
    tier_max: FetchTier = FetchTier.http
    pages_failed: int = 0
    bytes: int = 0
    error_category: ErrorCategory | None = None
    error: str | None = None
    robots_blocked: bool = False
