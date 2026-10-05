"""Crawler contracts shared by the crawler, extractors, enrichment resolvers and the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from scout.db.enums import ErrorCategory, FetchTier, PageType, WebsiteStatus


class PageLike(Protocol):
    """Anything that looks like a cached website page (ORM `WebsitePage` rows or `FetchedPage`)."""

    url: str
    page_type: PageType | str
    title: str | None
    content_text: str
    links: dict[str, Any]
    emails: list[Any]
    structured_data: list[Any]
    headings: list[Any]


@dataclass
class FetchedPage:
    url: str                      # requested URL
    final_url: str                # after redirects
    canonical_url: str            # scout.util.urls.canonical_url(final_url)
    status_code: int
    content_type: str | None
    page_type: PageType = PageType.other
    title: str | None = None
    meta_description: str | None = None
    content_text: str = ""        # cleaned visible text, ≤ 40 kB
    content_hash: str = ""        # scout.util.text.content_hash(content_text)
    language: str | None = None
    headers: dict[str, str] = field(default_factory=dict)   # lower-cased response headers
    head_html: str | None = None  # <head> + script/link tags (home page only), ≤ 48 kB
    links: dict[str, Any] = field(default_factory=dict)     # {"social": {"instagram": url, ...}, "internal": [{"url","text"}], "external": [...]}
    emails: list[str] = field(default_factory=list)          # lowercase addresses found (mailto + text)
    phones: list[str] = field(default_factory=list)
    structured_data: list[dict[str, Any]] = field(default_factory=list)  # JSON-LD objects
    headings: list[str] = field(default_factory=list)        # h1–h3 texts
    word_count: int = 0
    fetch_tier: FetchTier = FetchTier.http
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False    # 304 from a conditional request: reuse cached content


@dataclass
class CrawlResult:
    domain: str
    home_url: str | None
    status: WebsiteStatus                      # ok / unreachable / parked / blocked
    pages: list[FetchedPage] = field(default_factory=list)
    tier_max: FetchTier = FetchTier.http
    pages_failed: int = 0
    bytes: int = 0
    error_category: ErrorCategory | None = None
    error: str | None = None
    robots_blocked: bool = False
