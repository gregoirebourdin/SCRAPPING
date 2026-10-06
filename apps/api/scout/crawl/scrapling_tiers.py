"""Scrapling-backed fetch tiers (optional extra ``scraping``; docs/ARCHITECTURE.md §Crawl tiers).

* :class:`ScraplingFetcherTier` (L2) — ``scrapling.fetchers.FetcherSession`` (curl_cffi with a real
  browser TLS/HTTP2 fingerprint). Used only after L1 was blocked / challenged / answered an empty body.
  The underlying curl session is hardened before any request: environment proxies disabled,
  http/https only, libcurl redirects off (we follow hops ourselves, re-validating each one — curl_cffi
  < 0.15 had a redirect SSRF, GHSA-qw2m-4pqf-rmpp), body capped (CURLOPT_MAXFILESIZE) and every
  host:port **pinned** with CURLOPT_RESOLVE to the address validated by ``resolve_safe`` (no DNS
  rebinding window: curl never resolves a host itself).
* :class:`ScraplingDynamicTier` (L3) — ``AsyncDynamicSession`` (Playwright Chromium, one browser per
  crawl, page pool) made air-gapped by :class:`scout.crawl.browser_guard.BrowserGuard`: every browser
  request is served from the pinned L2 session, so the renderer itself never reaches the network.

Both degrade to ``None`` (logged once) when Scrapling, curl_cffi or the browsers are not installed,
when disabled in settings, or when the installed Scrapling no longer exposes what the hardening needs
(fail closed). Scrapling's StealthyFetcher / Cloudflare solver is deliberately not used (it disables
TLS verification and is a CAPTCHA bypass; we respect challenges we cannot pass with a real browser).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import ipaddress
import logging
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import structlog

from scout.config import get_settings
from scout.crawl import http
from scout.crawl.browser_guard import BROWSER_ARGS, CONTEXT_OPTIONS, DEAD_PROXY, BrowserGuard, RawResponse
from scout.crawl.ssrf import SSRFBlocked, resolve_safe, validate_url
from scout.db.enums import UsageCategory
from scout.services.usage import record_usage
from scout.util.pools import pool

log = structlog.get_logger(__name__)

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_ACCEPT_LANGUAGE = "fr,en;q=0.8,de;q=0.6,es;q=0.6,it;q=0.6,*;q=0.4"
DYNAMIC_POOL_WAIT_S = 5.0

_state: dict[str, Any] = {}  # "mod": loaded namespace | None, "warned": set[str]


def _warn_once(key: str, event: str, **kw: Any) -> None:
    warned: set[str] = _state.setdefault("warned", set())
    if key not in warned:
        warned.add(key)
        log.info(event, **kw)


def load_scrapling() -> Any | None:
    """Namespace with FetcherSession / AsyncDynamicSession / CurlOpt, or None (logged once)."""
    if "mod" in _state:
        return _state["mod"]
    mod: Any | None
    try:
        fetchers = importlib.import_module("scrapling.fetchers")
        curl_cffi = importlib.import_module("curl_cffi")
        mod = SimpleNamespace(
            FetcherSession=fetchers.FetcherSession,
            AsyncDynamicSession=fetchers.AsyncDynamicSession,
            CurlOpt=curl_cffi.CurlOpt,
        )
        logging.getLogger("scrapling").setLevel(logging.WARNING)  # it logs every fetch at INFO
    except Exception as exc:  # ImportError, or a broken optional install
        _warn_once("import", "scrapling_unavailable", error=str(exc)[:300])
        mod = None
    _state["mod"] = mod
    return mod


def reset_state() -> None:
    """Tests: forget the import probe and the once-only warnings."""
    _state.clear()


def _ascii_host(host: str) -> str:
    host = host.lower().strip(".")
    if host.isascii():
        return host
    return host.encode("idna").decode("ascii")


def _pick_address(addrs: list[tuple[str, int]]) -> tuple[str, int]:
    """Prefer IPv4 (many PaaS egress paths have no IPv6)."""
    for ip, port in addrs:
        with contextlib.suppress(ValueError):
            if ipaddress.ip_address(ip.split("%", 1)[0]).version == 4:
                return ip, port
    return addrs[0]


class ScraplingFetcherTier:
    """L2: Scrapling HTTP fetcher with browser impersonation, SSRF-hardened (see module docstring).

    One instance per crawl: the curl session (cookies, connection reuse) lives until :meth:`aclose`.
    """

    name = "scrapling_fetcher"

    def __init__(self) -> None:
        self._cm: Any = None
        self._session: Any = None
        self._curl: Any = None
        self._lock = asyncio.Lock()
        self._targets: dict[str, tuple[str, int]] = {}  # "host:port" → validated (ip, port)
        self._resolve: dict[str, str] = {}  # CURLOPT_RESOLVE entries (append-only)
        self._broken = False

    def available(self) -> bool:
        return bool(get_settings().scrapling_enabled) and not self._broken and load_scrapling() is not None

    # ---- session ------------------------------------------------------------------------------
    async def _ensure_session(self) -> Any:
        async with self._lock:
            if self._session is not None:
                return self._session
            mod = load_scrapling()
            if mod is None:
                raise RuntimeError("scrapling not installed")
            s = get_settings()
            impersonate = s.scrapling_impersonate or None
            cm = mod.FetcherSession(
                impersonate=impersonate,
                stealthy_headers=False,  # no fake Google referer / generated headers
                follow_redirects=False,
                retries=1,
                timeout=s.scrapling_timeout,
                verify=True,
                headers={} if impersonate else {"User-Agent": s.crawler_user_agent},
            )
            session = await cm.__aenter__()
            curl = getattr(session, "_async_curl_session", None)
            if curl is None or not hasattr(curl, "curl_options"):
                with contextlib.suppress(Exception):
                    await cm.__aexit__(None, None, None)
                self._broken = True
                _warn_once("compat", "scrapling_incompatible", reason="no hardenable curl session")
                raise RuntimeError("scrapling session cannot be hardened")
            self._cm, self._session, self._curl = cm, session, curl
            self._apply_curl_options()
            return session

    def _apply_curl_options(self) -> None:
        mod = load_scrapling()
        assert mod is not None and self._curl is not None
        opt = mod.CurlOpt
        options: dict[Any, Any] = {
            opt.PROXY: "",  # explicitly no proxy, even if HTTP(S)_PROXY is set in the environment
            opt.PROTOCOLS_STR: "http,https",
            opt.REDIR_PROTOCOLS_STR: "http,https",
            opt.FOLLOWLOCATION: 0,
            opt.MAXFILESIZE_LARGE: int(get_settings().crawler_max_bytes),
        }
        if self._resolve:
            options[opt.RESOLVE] = list(self._resolve.values())
        # A new dict each time: concurrent requests read a superset that always contains their pin.
        self._curl.curl_options = options

    async def _pin(self, url: str) -> tuple[str, dict[str, str]]:
        """Validate ``url``, resolve + validate its host once per crawl, pin it. Returns the URL curl
        must request (ASCII host) and extra headers. Raises SSRFBlocked / resolution errors."""
        validate_url(url)
        parts = urlsplit(url)
        host = _ascii_host(parts.hostname or "")
        scheme = parts.scheme.lower()
        default_port = 443 if scheme == "https" else 80
        port = parts.port or default_port
        key = f"{host}:{port}"
        if key not in self._targets:
            self._targets[key] = _pick_address(await resolve_safe(host, port))
        ip, real_port = self._targets[key]
        headers: dict[str, str] = {}
        netloc = host if port == default_port else f"{host}:{port}"
        if real_port != port:  # test-only host override pointing at another port
            netloc = f"{host}:{real_port}"
            headers["Host"] = host if port == default_port else f"{host}:{port}"
        pin_key = f"{host}:{real_port}"
        if pin_key not in self._resolve:
            literal = f"[{ip}]" if ":" in ip else ip
            self._resolve[pin_key] = f"{pin_key}:{literal}"
            self._apply_curl_options()
        return urlunsplit((scheme, netloc, parts.path or "/", parts.query, "")), headers

    # ---- fetching -----------------------------------------------------------------------------
    async def fetch_raw(self, url: str, headers: dict[str, str] | None = None) -> RawResponse | None:
        """GET with manual, re-validated redirects. None on refusal / failure (never raises)."""
        if not self.available():
            return None
        s = get_settings()
        try:
            session = await self._ensure_session()
        except Exception as exc:
            _warn_once("session", "scrapling_session_failed", error=str(exc)[:300])
            return None
        cap = int(s.crawler_max_bytes)
        current = url
        seen: set[str] = set()
        async with pool("scrapling"):
            for _hop in range(http.MAX_REDIRECTS + 1):
                if current in seen:
                    return None
                seen.add(current)
                try:
                    req_url, extra = await self._pin(current)
                except Exception as exc:  # SSRFBlocked, DNS failure, bad IDN
                    log.info("scrapling_fetch_refused", url=current[:300], error=str(exc)[:200])
                    return None
                await record_usage(UsageCategory.crawl_request, cost_usd=s.cost_crawl_request_usd)
                try:
                    resp = await asyncio.wait_for(
                        session.get(
                            req_url,
                            headers={"Accept-Language": _ACCEPT_LANGUAGE, **(headers or {}), **extra},
                            follow_redirects=False,
                            retries=1,
                        ),
                        timeout=s.scrapling_timeout + 5,
                    )
                except Exception as exc:  # CurlError (incl. size cap), timeouts, parser errors
                    log.info("scrapling_fetch_failed", url=current[:300], error=str(exc)[:200])
                    return None
                status = int(resp.status)
                resp_headers = {str(k).lower(): str(v) for k, v in dict(resp.headers).items()}
                location = resp_headers.get("location")
                if status in _REDIRECT_CODES and location:
                    current = urljoin(current, location.strip())
                    continue
                body = bytes(resp.body or b"")
                return RawResponse(status, resp_headers, body[:cap], current)
        log.info("scrapling_too_many_redirects", url=url[:300])
        return None

    async def fetch(self, url: str) -> http.HttpResponse | None:
        started = time.monotonic()
        raw = await self.fetch_raw(url, {"Accept": http.DEFAULT_ACCEPT})
        if raw is None:
            return None
        ctype = raw.headers.get("content-type")
        if not http._is_textual(ctype):
            return None
        return http.HttpResponse(
            url=url,
            final_url=raw.final_url,
            status_code=raw.status,
            headers=raw.headers,
            text=http.decode_body(raw.body, ctype),
            content_type=ctype,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            size_bytes=len(raw.body),
            tier=self.name,
        )

    async def aclose(self) -> None:
        cm, self._cm, self._session, self._curl = self._cm, None, None, None
        if cm is not None:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)


class ScraplingDynamicTier:
    """L3: Scrapling DynamicFetcher (Chromium) rendering, air-gapped behind :class:`BrowserGuard`.

    The browser session is started lazily on the first render of a crawl, reused for every page of
    that crawl (cookies + warm browser) and closed by :meth:`aclose`. It holds one slot of the
    ``scrapling_dynamic`` pool for its whole life, which bounds the number of live Chromium processes.
    """

    name = "scrapling_dynamic"

    def __init__(self, fetcher: ScraplingFetcherTier) -> None:
        self._fetcher = fetcher
        self._session: Any = None
        self._guard: BrowserGuard | None = None
        self._lock = asyncio.Lock()
        self._holds_slot = False
        self._failed = False

    def available(self) -> bool:
        if not get_settings().scrapling_dynamic_enabled or self._failed or _state.get("no_browser"):
            return False
        return self._fetcher.available()

    async def _ensure(self) -> tuple[Any, BrowserGuard] | None:
        async with self._lock:
            if self._session is not None and self._guard is not None:
                return self._session, self._guard
            if self._failed:
                return None
            mod = load_scrapling()
            if mod is None:
                return None
            sem = pool("scrapling_dynamic")
            try:
                await asyncio.wait_for(sem.acquire(), timeout=DYNAMIC_POOL_WAIT_S)
            except TimeoutError:
                self._failed = True  # don't wait again on every page of this crawl
                log.info("scrapling_dynamic_pool_busy")
                return None
            self._holds_slot = True
            s = get_settings()
            session = None
            try:
                session = mod.AsyncDynamicSession(
                    headless=True,
                    google_search=False,
                    disable_resources=False,  # the guard blocks resources (page routes would shadow it)
                    network_idle=True,
                    retries=1,
                    max_pages=2,
                    timeout=int(s.scrapling_dynamic_timeout * 1000),
                    proxy=DEAD_PROXY,
                    extra_flags=list(BROWSER_ARGS),
                    additional_args=dict(CONTEXT_OPTIONS),
                )
                await session.start()
                if session.context is None:
                    raise RuntimeError("no browser context to guard")
                guard = BrowserGuard(self._fetcher.fetch_raw)
                await guard.install(session.context)
            except Exception as exc:
                self._failed = True
                msg = str(exc)
                if "Executable doesn't exist" in msg or "playwright install" in msg:
                    _state["no_browser"] = True
                    _warn_once("browser", "scrapling_dynamic_no_browser", hint="run `scrapling install`")
                else:
                    log.info("scrapling_dynamic_start_failed", error=msg[:300])
                if session is not None:
                    with contextlib.suppress(Exception):
                        await session.close()
                self._release()
                return None
            self._session, self._guard = session, guard
            return session, guard

    def _release(self) -> None:
        if self._holds_slot:
            self._holds_slot = False
            pool("scrapling_dynamic").release()

    async def fetch(self, url: str) -> http.HttpResponse | None:
        if not self.available():
            return None
        try:
            validate_url(url)
        except SSRFBlocked:
            return None
        ready = await self._ensure()
        if ready is None:
            return None
        session, guard = ready
        s = get_settings()
        started = time.monotonic()
        await record_usage(UsageCategory.browser_request, cost_usd=s.cost_browser_request_usd)
        guard.begin(url)
        try:
            resp = await asyncio.wait_for(session.fetch(url), timeout=s.scrapling_dynamic_timeout + 10)
        except Exception as exc:
            log.info("scrapling_dynamic_failed", url=url[:300], error=str(exc)[:300])
            return None
        if not guard.served(url):  # the navigation did not go through the guard: fail closed
            log.warning("scrapling_dynamic_unguarded_navigation", url=url[:300])
            return None
        final_url = str(getattr(resp, "url", "") or url)
        try:
            validate_url(final_url)
        except SSRFBlocked:
            return None
        html = bytes(resp.body or b"").decode("utf-8", errors="replace")
        headers = {str(k).lower(): str(v) for k, v in dict(getattr(resp, "headers", {}) or {}).items()}
        return http.HttpResponse(
            url=url,
            final_url=final_url,
            status_code=int(resp.status),
            headers=headers,
            text=html,
            content_type=headers.get("content-type") or "text/html",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            size_bytes=len(html),
            tier=self.name,
        )

    async def aclose(self) -> None:
        session, self._session, self._guard = self._session, None, None
        try:
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.close()
        finally:
            self._release()
