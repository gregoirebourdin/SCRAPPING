"""SSRF policy: IP classification, URL validation, connect-time (rebinding-safe) checks, redirects."""

from __future__ import annotations

import pytest

from scout.crawl import http as crawl_http
from scout.crawl import ssrf
from scout.crawl.ssrf import SSRFBlocked, is_public_ip, resolve_safe, validate_url
from tests.unit.crawl.fixture_server import configure_overrides


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1", "127.8.8.8", "10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.1.1", "100.64.0.1",
        "100.127.255.254", "169.254.169.254", "169.254.0.1", "0.0.0.0", "224.0.0.1", "239.255.255.250",
        "240.0.0.1", "255.255.255.255", "198.18.0.1", "192.0.2.10", "::1", "::", "fc00::1", "fd12:3456::1",
        "fe80::1", "fec0::1", "ff02::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",
        "64:ff9b::a00:1", "2002:a00:1::1", "2001:db8::1", "not-an-ip", "",
    ],
)
def test_non_public_ips_are_blocked(ip):
    assert not is_public_ip(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_ips_are_allowed(ip):
    assert is_public_ip(ip)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.fr/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://10.0.0.1:8080/",
        "http://100.64.1.1/",
        "http://example.fr:22/",
        "http://example.fr:6379/",
        "http://user:pass@example.fr/",
        "http://localhost/",
        "http://api.localhost/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://printer.local/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http:///nohost",
    ],
)
def test_validate_url_rejects(url):
    with pytest.raises(SSRFBlocked):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://agence-lumiere.fr/", "http://example.com:8080/x", "https://example.com:8443/", "https://93.184.216.34/"],
)
def test_validate_url_accepts(url):
    validate_url(url)


async def test_resolution_to_private_address_is_blocked(monkeypatch):
    async def fake_getaddrinfo(host, port):
        return [("93.184.216.34", port), ("10.0.0.5", port)]

    monkeypatch.setattr(ssrf, "_getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SSRFBlocked):
        await resolve_safe("rebind.example.com", 80)


async def test_dns_rebinding_blocked_at_connect_time(monkeypatch):
    """validate_url passes (public-looking host) but the connection backend refuses the private IP."""

    async def fake_getaddrinfo(host, port):
        return [("169.254.169.254", port)]

    monkeypatch.setattr(ssrf, "_getaddrinfo", fake_getaddrinfo)
    validate_url("http://rebind.example.com/")
    with pytest.raises(SSRFBlocked):
        await crawl_http.fetch("http://rebind.example.com/")
    await crawl_http.close_client()


async def test_public_resolution_passes(monkeypatch):
    async def fake_getaddrinfo(host, port):
        return [("93.184.216.34", port)]

    monkeypatch.setattr(ssrf, "_getaddrinfo", fake_getaddrinfo)
    assert await resolve_safe("example.com", 443) == [("93.184.216.34", 443)]


async def test_redirect_to_private_ip_is_blocked(fixture_server, monkeypatch):
    fixture_server.redirect("redirector.fr", "/", "http://169.254.169.254/latest/meta-data/")
    fixture_server.redirect("redirector.fr", "/local", f"http://localhost:{fixture_server.port}/")
    fixture_server.redirect("redirector.fr", "/loopback", f"http://127.0.0.1:{fixture_server.port}/")
    configure_overrides(monkeypatch, {"redirector.fr": fixture_server.target()})
    for path in ("/", "/local", "/loopback"):
        with pytest.raises(SSRFBlocked):
            await crawl_http.fetch(f"http://redirector.fr{path}")


async def test_private_target_allowed_only_with_test_overrides(fixture_server, monkeypatch):
    fixture_server.add("127.0.0.1", "/", "<html><body><p>local</p></body></html>")
    direct = f"http://127.0.0.1:{fixture_server.port}/"
    with pytest.raises(SSRFBlocked):
        await crawl_http.fetch(direct)
    configure_overrides(monkeypatch, {}, allow_private=["127.0.0.1"])
    resp = await crawl_http.fetch(direct)
    assert resp.status_code == 200 and "local" in resp.text


async def test_host_override_reaches_local_server(fixture_server, monkeypatch):
    fixture_server.add("agence-lumiere.fr", "/", "<html><body><h1>Bonjour</h1></body></html>")
    configure_overrides(monkeypatch, {"agence-lumiere.fr": fixture_server.target()})
    resp = await crawl_http.fetch("http://agence-lumiere.fr/")
    assert resp.status_code == 200
    assert "Bonjour" in resp.text
    assert fixture_server.requests[-1][0] == "agence-lumiere.fr"
