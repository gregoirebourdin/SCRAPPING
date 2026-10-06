"""HTTP fetch: status/error mapping, redirects, conditional requests, size caps, charset decoding."""

from __future__ import annotations

import socket

import pytest

from scout.crawl import http as crawl_http
from scout.crawl.http import HttpBlockedError, HttpRateLimitedError, decode_body
from scout.db.enums import ErrorCategory
from scout.errors import BlockedError, FetchError, PermanentError, RateLimitedError
from tests.unit.crawl.fixture_server import configure_overrides

HOST = "site-test.fr"


@pytest.fixture
def site(fixture_server, monkeypatch):
    configure_overrides(monkeypatch, {HOST: fixture_server.target()})
    return fixture_server


async def test_ok_response_lowercases_headers_and_records_etag(site):
    site.add(HOST, "/", "<html><body>ok</body></html>", headers={"X-Custom": "Yes"})
    resp = await crawl_http.fetch(f"http://{HOST}/")
    assert resp.ok and resp.status_code == 200
    assert resp.headers["x-custom"] == "Yes"
    assert resp.etag and resp.etag.startswith('"')
    assert resp.content_type.startswith("text/html")
    assert resp.final_url == f"http://{HOST}/"


async def test_404_and_410_are_returned_not_raised(site):
    resp = await crawl_http.fetch(f"http://{HOST}/missing")
    assert resp.status_code == 404
    site.add(HOST, "/gone", "gone", status=410)
    assert (await crawl_http.fetch(f"http://{HOST}/gone")).status_code == 410


async def test_403_is_blocked(site):
    site.add(HOST, "/", "forbidden", status=403)
    with pytest.raises(HttpBlockedError) as exc:
        await crawl_http.fetch(f"http://{HOST}/")
    assert isinstance(exc.value, BlockedError)
    assert exc.value.status_code == 403
    assert exc.value.category == ErrorCategory.blocked


async def test_429_is_rate_limited(site):
    site.add(HOST, "/", "slow down", status=429, headers={"Retry-After": "30"})
    with pytest.raises(HttpRateLimitedError) as exc:
        await crawl_http.fetch(f"http://{HOST}/")
    assert isinstance(exc.value, RateLimitedError)
    assert exc.value.retry_after == 30


async def test_503_cloudflare_challenge_is_blocked(site):
    site.add(
        HOST, "/", "<html><head><title>Just a moment...</title></head><body>cf</body></html>", status=503
    )
    with pytest.raises(BlockedError):
        await crawl_http.fetch(f"http://{HOST}/")
    site.add(HOST, "/cf", "<html>challenge</html>", status=200, headers={"cf-mitigated": "challenge"})
    with pytest.raises(BlockedError):
        await crawl_http.fetch(f"http://{HOST}/cf")
    site.add(HOST, "/down", "maintenance", status=503)
    assert (await crawl_http.fetch(f"http://{HOST}/down")).status_code == 503


async def test_connection_refused_is_network_fetch_error(monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    configure_overrides(monkeypatch, {"dead-site.fr": f"127.0.0.1:{port}"})
    with pytest.raises(FetchError) as exc:
        await crawl_http.fetch("http://dead-site.fr/")
    assert exc.value.category in (ErrorCategory.network, ErrorCategory.timeout)


async def test_redirects_are_followed_and_recorded(site):
    site.redirect(HOST, "/old", "/new")
    site.redirect(HOST, "/new", f"http://{HOST}/final", status=302)
    site.add(HOST, "/final", "<p>final</p>")
    resp = await crawl_http.fetch(f"http://{HOST}/old")
    assert resp.final_url == f"http://{HOST}/final"
    assert resp.redirects == [f"http://{HOST}/new", f"http://{HOST}/final"]
    assert resp.url == f"http://{HOST}/old"


async def test_too_many_redirects(site):
    for i in range(8):
        site.redirect(HOST, f"/r{i}", f"/r{i + 1}")
    with pytest.raises(PermanentError):
        await crawl_http.fetch(f"http://{HOST}/r0")


async def test_conditional_request_returns_not_modified(site):
    site.add(HOST, "/page", "<p>stable</p>")
    first = await crawl_http.fetch(f"http://{HOST}/page")
    second = await crawl_http.fetch(f"http://{HOST}/page", etag=first.etag)
    assert second.not_modified and second.status_code == 304 and second.text == ""
    assert site.requests[-1][2].get("if-none-match") == first.etag


async def test_body_is_capped(site):
    site.add(HOST, "/big", "<p>" + "x" * 50_000 + "</p>")
    resp = await crawl_http.fetch(f"http://{HOST}/big", max_bytes=1000)
    assert resp.truncated and resp.size_bytes == 1000 and len(resp.text) == 1000


async def test_binary_content_is_not_downloaded(site):
    site.add(HOST, "/doc.pdf", b"%PDF-1.7 binary", content_type="application/pdf")
    resp = await crawl_http.fetch(f"http://{HOST}/doc.pdf")
    assert resp.status_code == 200 and resp.text == "" and resp.size_bytes == 0


async def test_latin1_page_is_decoded(site):
    body = "<html><head><meta charset='iso-8859-1'></head><body>Équipe créative</body></html>".encode(
        "latin-1"
    )
    site.add(HOST, "/latin", body, content_type="text/html")
    resp = await crawl_http.fetch(f"http://{HOST}/latin")
    assert "Équipe créative" in resp.text


def test_decode_body_fallbacks():
    assert decode_body("café".encode(), "text/html; charset=utf-8") == "café"
    assert decode_body("café".encode("cp1252"), None) == "café"
    assert decode_body(b"\xef\xbb\xbfhello", None) == "hello"
    assert decode_body(b"", None) == ""
