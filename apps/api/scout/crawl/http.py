"""SSRF-safe HTTP fetching (crawler tier L1).

``fetch()`` follows redirects manually (max 5, every hop re-validated), caps the body size while
streaming, decodes with charset detection, supports conditional requests and maps transport /
anti-bot failures onto ``scout.errors`` categories. Every request is recorded as
``UsageCategory.crawl_request`` (a no-op outside a usage scope).
"""

from __future__ import annotations

import asyncio
import codecs
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
import structlog

from scout.config import get_settings
from scout.crawl.ssrf import SafeAsyncTransport, validate_url
from scout.db.enums import ErrorCategory, UsageCategory
from scout.errors import BlockedError, FetchError, PermanentError, RateLimitedError
from scout.services.usage import record_usage
from scout.util.pools import pool

log = structlog.get_logger(__name__)

MAX_REDIRECTS = 5
DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_TEXTUAL_TYPES = (
    "text/",
    "application/xhtml+xml",
    "application/xml",
    "application/json",
    "application/ld+json",
    "application/rss+xml",
    "application/atom+xml",
)
_CHALLENGE_MARKERS = (
    "just a moment...",
    "cf-browser-verification",
    "challenge-platform",
    "cf-chl-",
    "attention required! | cloudflare",
    "ddos-guard",
    "checking your browser before accessing",
    "please enable cookies",
    "_incapsula_resource",
    "px-captcha",
)
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-:.]+)""", re.IGNORECASE)


class HttpBlockedError(BlockedError):
    """Anti-bot / forbidden response. Carries the HTTP status code."""

    def __init__(self, message: str, *, status_code: int, url: str):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class HttpRateLimitedError(RateLimitedError):
    """HTTP 429. Carries the server-provided Retry-After (seconds) when parseable."""

    def __init__(self, message: str, *, url: str, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = 429
        self.url = url
        self.retry_after = retry_after


@dataclass
class HttpResponse:
    url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    text: str
    content_type: str | None
    elapsed_ms: int
    redirects: list[str] = field(default_factory=list)
    not_modified: bool = False
    size_bytes: int = 0
    truncated: bool = False
    tier: str = "http"  # fetch tier that produced it (scout.crawl.tiers telemetry key)
    body: bytes = b""  # raw bytes, only kept by ``fetch(raw=True)`` (browser guard)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")


# ---- client lifecycle ------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def get_client() -> httpx.AsyncClient:
    """Process-wide AsyncClient bound to the SSRF-safe transport (recreated if the loop changed)."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is not None and not _client.is_closed and _client_loop is loop:
        return _client
    s = get_settings()
    _client = httpx.AsyncClient(
        transport=SafeAsyncTransport(
            limits=httpx.Limits(
                max_connections=max(10, s.pool_http * 2),
                max_keepalive_connections=max(5, s.pool_http),
                keepalive_expiry=10.0,
            )
        ),
        headers={
            "User-Agent": s.crawler_user_agent,
            "Accept-Language": "fr,en;q=0.8,de;q=0.6,es;q=0.6,it;q=0.6,*;q=0.4",
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=httpx.Timeout(
            connect=s.crawler_connect_timeout,
            read=s.crawler_read_timeout,
            write=s.crawler_read_timeout,
            pool=s.crawler_connect_timeout + s.crawler_read_timeout,
        ),
        follow_redirects=False,
        trust_env=False,
    )
    _client_loop = loop
    return _client


async def close_client() -> None:
    global _client, _client_loop
    client, _client, _client_loop = _client, None, None
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except RuntimeError:  # loop already closed (tests)
            pass


# ---- decoding ---------------------------------------------------------------------------------


def _charset_from_content_type(ct: str | None) -> str | None:
    if not ct:
        return None
    m = re.search(r"charset\s*=\s*[\"']?([\w\-:.]+)", ct, re.IGNORECASE)
    return m.group(1) if m else None


def _valid_codec(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return codecs.lookup(name.strip().lower()).name
    except LookupError:
        return None


def decode_body(body: bytes, content_type: str | None) -> str:
    """Decode bytes: BOM → Content-Type charset → <meta charset> → UTF-8 → cp1252."""
    if not body:
        return ""
    for bom, enc in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if body.startswith(bom):
            return body.decode(enc, errors="replace")
    declared = _valid_codec(_charset_from_content_type(content_type))
    if declared is None:
        m = _META_CHARSET_RE.search(body[:4096])
        declared = _valid_codec(m.group(1).decode("ascii", "ignore") if m else None)
    if declared:
        try:
            return body.decode(declared)
        except (UnicodeDecodeError, LookupError):
            pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


def _is_textual(content_type: str | None) -> bool:
    if not content_type:
        return True
    ct = content_type.split(";", 1)[0].strip().lower()
    return ct.startswith(_TEXTUAL_TYPES) or ct.endswith(("+xml", "+json"))


def _looks_like_challenge(headers: dict[str, str], body_head: str) -> bool:
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    low = body_head[:20000].lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


async def _read_capped(resp: httpx.Response, cap: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        remaining = cap - total
        if len(chunk) >= remaining:
            chunks.append(chunk[:remaining])
            return b"".join(chunks), True
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), False


# ---- fetch ------------------------------------------------------------------------------------


async def fetch(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    max_bytes: int | None = None,
    accept: str = DEFAULT_ACCEPT,
    raw: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> HttpResponse:
    """GET ``url`` safely. 404/410 (and other non-blocking statuses) are returned to the caller.

    ``raw=True`` reads the (capped) body whatever its content type and keeps the bytes in
    ``HttpResponse.body`` (browser guard serving scripts/styles); ``extra_headers`` are added as-is.

    Raises SSRFBlocked (policy), FetchError (network/timeout), HttpBlockedError (403, challenge),
    HttpRateLimitedError (429), PermanentError (redirect loop / too many redirects).
    """
    settings = get_settings()
    cap = max_bytes or settings.crawler_max_bytes
    headers = {**(extra_headers or {}), "Accept": accept}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    client = get_client()
    started = time.monotonic()
    redirects: list[str] = []
    current = url
    seen: set[str] = set()

    async with pool("http"):
        for _hop in range(MAX_REDIRECTS + 1):
            validate_url(current)
            if current in seen:
                raise PermanentError(f"redirect loop at {current}", category=ErrorCategory.network)
            seen.add(current)
            try:
                request = client.build_request("GET", current, headers=headers)
                resp = await client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise FetchError(
                    f"timeout fetching {current}: {exc!r}", category=ErrorCategory.timeout
                ) from exc
            except httpx.InvalidURL as exc:
                raise PermanentError(
                    f"invalid URL {current}: {exc}", category=ErrorCategory.validation
                ) from exc
            except (httpx.HTTPError, OSError) as exc:
                raise FetchError(f"network error fetching {current}: {exc!r}") from exc
            finally:
                await record_usage(UsageCategory.crawl_request, cost_usd=settings.cost_crawl_request_usd)

            try:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                status = resp.status_code
                if status in _REDIRECT_CODES and resp_headers.get("location"):
                    nxt = urljoin(current, resp_headers["location"].strip())
                    redirects.append(nxt)
                    current = nxt
                    continue
                content_type = resp_headers.get("content-type")
                body = b""
                truncated = False
                if status != 304 and (raw or _is_textual(content_type)):
                    try:
                        body, truncated = await _read_capped(resp, cap)
                    except httpx.TimeoutException as exc:
                        raise FetchError(f"read timeout {current}", category=ErrorCategory.timeout) from exc
                    except httpx.HTTPError as exc:
                        raise FetchError(f"read error {current}: {exc!r}") from exc
            finally:
                await resp.aclose()

            text = decode_body(body, content_type)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if status == 429:
                raise HttpRateLimitedError(
                    f"rate limited (429) by {current}",
                    url=current,
                    retry_after=_retry_after(resp_headers.get("retry-after")),
                )
            challenged = resp_headers.get("cf-mitigated", "").lower() == "challenge" or (
                status in (401, 503) and _looks_like_challenge(resp_headers, text)
            )
            if status == 403 or challenged:
                raise HttpBlockedError(f"blocked ({status}) by {current}", status_code=status, url=current)
            if truncated:
                log.info("crawl_body_truncated", url=current, cap=cap)
            return HttpResponse(
                url=url,
                final_url=current,
                status_code=status,
                headers=resp_headers,
                text=text,
                content_type=content_type,
                elapsed_ms=elapsed_ms,
                redirects=redirects,
                not_modified=status == 304,
                size_bytes=len(body),
                truncated=truncated,
                body=body if raw else b"",
            )
    raise PermanentError(f"too many redirects (> {MAX_REDIRECTS}) from {url}", category=ErrorCategory.network)
