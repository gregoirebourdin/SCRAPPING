"""Tiny asyncio HTTP/1.1 server on 127.0.0.1 serving fixture sites by Host header.

Used with ``crawler_host_overrides`` (host → 127.0.0.1:<port>) so the real SSRF-safe client is
exercised end to end without touching the internet. Supports ETag / If-None-Match (304),
redirects and request logging. TLS handshakes are refused immediately (connection closed), which
lets tests exercise the https → http fallback quickly.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "html"


@dataclass
class Route:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "text/html; charset=utf-8"
    etag: bool = True


class FixtureServer:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Route] = {}
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    # ---- routes ---------------------------------------------------------------------------
    def add(
        self,
        host: str,
        path: str,
        body: str | bytes = b"",
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
        etag: bool = True,
    ) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.routes[(host.lower(), path)] = Route(status, data, headers or {}, content_type, etag)

    def redirect(self, host: str, path: str, location: str, status: int = 301) -> None:
        self.add(host, path, b"", status=status, headers={"Location": location}, etag=False)

    def add_site(self, host: str, pages: dict[str, str]) -> None:
        """``pages``: path → fixture file name (relative to tests/fixtures/html)."""
        for path, name in pages.items():
            file = FIXTURES / name
            ct = "text/html; charset=utf-8"
            if name.endswith(".txt"):
                ct = "text/plain; charset=utf-8"
            elif name.endswith(".xml"):
                ct = "application/xml"
            self.add(host, path, file.read_bytes(), content_type=ct)

    def count(self, host: str | None = None) -> int:
        return sum(1 for h, _p, _hd in self.requests if host is None or h == host)

    # ---- lifecycle --------------------------------------------------------------------------
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    def target(self) -> str:
        return f"127.0.0.1:{self.port}"

    # ---- protocol ----------------------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(reader.read(1), timeout=5)
            if not first or first == b"\x16":  # TLS ClientHello → refuse
                return
            rest = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            head = (first + rest).decode("latin-1")
            request_line, *header_lines = head.split("\r\n")
            _method, target, _version = request_line.split(" ", 2)
            headers = {}
            for line in header_lines:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            host = headers.get("host", "").split(":")[0].lower()
            path = target.split("?", 1)[0] or "/"
            self.requests.append((host, path, headers))
            route = self.routes.get((host, path)) or self.routes.get((host, path.rstrip("/") or "/"))
            if route is None:
                status, body, extra, ctype = (
                    404,
                    b"<html><body><h1>Not found</h1></body></html>",
                    {},
                    "text/html",
                )
            else:
                status, body, extra, ctype = route.status, route.body, dict(route.headers), route.content_type
                if route.etag and status == 200:
                    etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
                    extra["ETag"] = etag
                    if headers.get("if-none-match") == etag:
                        status, body = 304, b""
            reason = {
                200: "OK",
                301: "Moved Permanently",
                302: "Found",
                304: "Not Modified",
                403: "Forbidden",
                404: "Not Found",
                429: "Too Many Requests",
                503: "Service Unavailable",
            }.get(status, "OK")
            lines = [f"HTTP/1.1 {status} {reason}", f"Content-Length: {len(body)}", "Connection: close"]
            if status != 304:
                lines.append(f"Content-Type: {ctype}")
            lines += [f"{k}: {v}" for k, v in extra.items()]
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
            await writer.drain()
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()


AGENCE_LUMIERE_PAGES = {
    "/": "agence_lumiere/home.html",
    "/agence": "agence_lumiere/agence.html",
    "/equipe": "agence_lumiere/equipe.html",
    "/services": "agence_lumiere/services.html",
    "/contact": "agence_lumiere/contact.html",
    "/mentions-legales": "agence_lumiere/mentions-legales.html",
    "/blog": "agence_lumiere/blog.html",
    "/robots.txt": "agence_lumiere/robots.txt",
    "/sitemap.xml": "agence_lumiere/sitemap.xml",
}


def configure_overrides(
    monkeypatch, hosts: dict[str, str], *, allow_private: list[str] | None = None
) -> None:
    """Point hosts at local targets and reset cached settings / robots / HTTP client."""
    from scout.config import get_settings
    from scout.crawl import robots

    monkeypatch.setenv("CRAWLER_HOST_OVERRIDES", json.dumps(hosts))
    if allow_private is not None:
        monkeypatch.setenv("CRAWLER_ALLOW_PRIVATE_HOSTS", json.dumps(allow_private))
    get_settings.cache_clear()
    robots.clear_cache()


async def reset_crawl_state() -> None:
    from scout.config import get_settings
    from scout.crawl import robots
    from scout.crawl.http import close_client

    for var in ("CRAWLER_HOST_OVERRIDES", "CRAWLER_ALLOW_PRIVATE_HOSTS"):
        os.environ.pop(var, None)
    get_settings.cache_clear()
    robots.clear_cache()
    await close_client()
