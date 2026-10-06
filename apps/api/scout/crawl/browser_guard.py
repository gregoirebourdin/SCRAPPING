"""Air-gapped browser guard shared by every guarded browser tier (Scrapling DynamicFetcher, Playwright).

Invariant: **the browser never touches the network itself.** Every request it makes is intercepted by a
context-level route and served with ``route.fulfill`` from one of our SSRF-safe fetchers (L1 httpx with
the rebinding-proof transport, or the Scrapling L2 session with pinned IPs). Those fetchers validate
the URL, resolve + validate every address, pin the connection to the validated IP, follow redirects
themselves (re-validating every hop) and cap the body. Requests we refuse are aborted.

Why not "validate then ``route.continue_()``" (the previous Playwright tier)? Verified with Chromium
153 / Playwright 1.63: route handlers are **not** called for redirect follow-ups — neither for a 3xx
coming from the network nor for a 3xx we fulfill ourselves — so a public page answering
``302 Location: http://169.254.169.254/`` made the browser issue that request unseen (blind SSRF), and
the browser re-resolves DNS itself (rebinding window). Serving every response ourselves closes both.

Defence in depth for traffic that bypasses routing (preconnect / DNS prefetch / WebRTC / workers):
the browser is launched with a dead proxy (Playwright forces ``<-loopback>`` so loopback is proxied too),
every host name maps to NOTFOUND, WebRTC is restricted to proxied UDP, service workers are blocked and
WebSockets are refused (never connected to a server).
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from scout.crawl import http
from scout.crawl.ssrf import SSRFBlocked, validate_url
from scout.errors import JobError

log = structlog.get_logger(__name__)

DEAD_PROXY = "http://127.0.0.1:9"  # discard port: nothing listens, every proxied connection fails
BROWSER_ARGS: tuple[str, ...] = (
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--force-webrtc-ip-handling-policy",
    "--disable-background-networking",
    "--dns-prefetch-disable",
)
CONTEXT_OPTIONS: dict[str, Any] = {"service_workers": "block", "accept_downloads": False}
BLOCKED_RESOURCE_TYPES = frozenset(
    {"image", "imageset", "font", "media", "texttrack", "beacon", "csp_report", "object", "ping"}
)
MAX_REQUESTS_PER_PAGE = 200
_FORWARD_REQUEST_HEADERS = ("cookie", "referer")
_DROP_RESPONSE_HEADERS = frozenset(
    {
        "content-length",
        "content-encoding",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "alt-svc",
        "location",
        "upgrade",
        "proxy-authenticate",
        "strict-transport-security",
    }
)


@dataclass
class RawResponse:
    """What a guard fetcher hands back: the final (post-redirect) response, body already decoded
    from any Content-Encoding and capped."""

    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str


FetchBytes = Callable[[str, dict[str, str]], Awaitable[RawResponse | None]]


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


async def http_raw_fetch(url: str, headers: dict[str, str]) -> RawResponse | None:
    """Guard fetcher backed by the L1 httpx client (any content type, capped, redirects re-validated)."""
    accept = headers.get("accept") or "*/*"
    fwd = {k: v for k, v in headers.items() if k != "accept"}
    try:
        resp = await http.fetch(url, raw=True, accept=accept, extra_headers=fwd)
    except (JobError, SSRFBlocked) as exc:
        log.debug("browser_guard_fetch_refused", url=url, error=str(exc)[:200])
        return None
    return RawResponse(resp.status_code, resp.headers, resp.body, resp.final_url)


async def _refuse_websocket(ws: Any) -> None:
    """Never connect page WebSockets to a server (not mocked either: just closed)."""
    with contextlib.suppress(Exception):
        await ws.close()


class BrowserGuard:
    """Context-level route handler serving every browser request from ``fetch``.

    ``begin(url)`` before each top-level navigation; ``served(url)`` afterwards proves that the
    navigation really went through the guard (fail closed if it did not).
    """

    def __init__(self, fetch: FetchBytes, *, max_requests: int = MAX_REQUESTS_PER_PAGE) -> None:
        self._fetch = fetch
        self._max_requests = max_requests
        self._documents: set[str] = set()
        self._count = 0
        self.fulfilled = 0
        self.refused = 0

    async def install(self, context: Any) -> None:
        """Route every request of ``context``. Raises if any part cannot be installed."""
        await context.route("**/*", self.handle)
        route_ws = getattr(context, "route_web_socket", None)
        if route_ws is None:  # Playwright < 1.48: WebSockets could reach the network
            raise RuntimeError("playwright too old to refuse WebSockets (route_web_socket missing)")
        await route_ws("**/*", _refuse_websocket)

    def begin(self, url: str) -> None:
        self._count = 0
        self._documents.discard(_strip_fragment(url))

    def served(self, url: str) -> bool:
        return _strip_fragment(url) in self._documents

    async def _refuse(self, route: Any, reason: str, url: str) -> None:
        self.refused += 1
        log.debug("browser_guard_refused", url=url[:300], reason=reason)
        with contextlib.suppress(Exception):
            await route.abort()

    async def handle(self, route: Any) -> None:
        req = route.request
        url = str(getattr(req, "url", ""))
        try:
            if req.method != "GET":
                return await self._refuse(route, f"method {req.method}", url)
            if req.resource_type in BLOCKED_RESOURCE_TYPES:
                return await self._refuse(route, f"resource {req.resource_type}", url)
            try:
                validate_url(url)
            except SSRFBlocked as exc:
                return await self._refuse(route, str(exc), url)
            if self._count >= self._max_requests:
                return await self._refuse(route, "request budget exhausted", url)
            self._count += 1
            sent = await req.all_headers()
            headers = {k: sent[k] for k in _FORWARD_REQUEST_HEADERS if sent.get(k)}
            headers["accept"] = sent.get("accept") or "*/*"
            res = await self._fetch(url, headers)
            if res is None:
                return await self._refuse(route, "fetch failed or refused", url)
            out_headers = {k: v for k, v in res.headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS}
            status = res.status if 200 <= res.status < 600 and not 300 <= res.status < 400 else 502
            await route.fulfill(status=status, headers=out_headers, body=res.body)
            self.fulfilled += 1
            if req.resource_type == "document":
                self._documents.add(_strip_fragment(url))
        except Exception as exc:  # never let a handler error leave a request pending
            log.debug("browser_guard_error", url=url[:300], error=str(exc)[:300])
            with contextlib.suppress(Exception):
                await route.abort()
