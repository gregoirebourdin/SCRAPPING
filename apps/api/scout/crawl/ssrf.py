"""SSRF protection for every outbound crawl request (docs/SECURITY.md §5).

Two layers:

* :func:`validate_url` — cheap pre-flight checks (scheme, port, credentials, raw private IPs,
  internal-only host names). Run before the first request and again on every redirect hop.
* :class:`SafeNetworkBackend` — an httpcore network backend that resolves the host itself,
  validates **every** resolved address and connects to the validated IP. Because the check
  happens at connect time with the very address used for the socket, DNS rebinding cannot slip
  a private address in between validation and connection. TLS SNI / certificate checks still use
  the original host name (httpcore calls ``start_tls(server_hostname=<origin host>)``).

Test-only escape hatches (refused in production by ``Settings.assert_safe``):
``crawler_host_overrides`` (exact host → ``"ip:port"``) and ``crawler_allow_private_hosts``
(host names, IPs or CIDR networks allowed to resolve to non-public addresses).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx
import structlog

from scout.config import get_settings
from scout.db.enums import ErrorCategory
from scout.errors import PermanentError

log = structlog.get_logger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
DNS_TIMEOUT_S = 5.0

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Explicit deny-list (in addition to `not ip.is_global`) so the policy is readable and stable
# across Python versions.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "this" network / unspecified
        "10.0.0.0/8",  # RFC 1918
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, incl. 169.254.169.254 cloud metadata
        "172.16.0.0/12",  # RFC 1918
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "192.88.99.0/24",  # 6to4 relay anycast
        "192.168.0.0/16",  # RFC 1918
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved
        "255.255.255.255/32",  # broadcast
        "::/128",  # unspecified
        "::1/128",  # loopback
        "100::/64",  # discard-only
        "2001::/23",  # IETF protocol assignments (incl. Teredo 2001::/32)
        "2001:db8::/32",  # documentation
        "fc00::/7",  # unique local (ULA)
        "fe80::/10",  # link-local
        "fec0::/10",  # deprecated site-local
        "ff00::/8",  # multicast
    )
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

# Host names that only make sense inside private infrastructure.
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa", ".corp")
_INTERNAL_HOSTS = frozenset({"localhost", "metadata", "metadata.google.internal", "instance-data"})


class SSRFBlocked(PermanentError):
    """The URL or one of its resolved addresses is not allowed (never retried)."""

    category = ErrorCategory.validation


def _parse_ip(ip: Any) -> IPAddress | None:
    if isinstance(ip, ipaddress.IPv4Address | ipaddress.IPv6Address):
        return ip
    try:
        return ipaddress.ip_address(str(ip).strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def is_public_ip(ip: Any) -> bool:
    """True only for globally routable unicast addresses; anything unparsable is not public."""
    addr = _parse_ip(ip)
    if addr is None:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:  # ::ffff:10.0.0.1
            return is_public_ip(addr.ipv4_mapped)
        if addr.sixtofour is not None:  # 2002:0a00:0001::/48 embeds 10.0.0.1
            return is_public_ip(addr.sixtofour)
        if addr in _NAT64:  # 64:ff9b::a00:1 embeds 10.0.0.1
            return is_public_ip(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    if any(addr in net for net in _BLOCKED_NETWORKS if net.version == addr.version):
        return False
    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return False
    return bool(addr.is_global)


def _host_allowed_private(host: str, ip: IPAddress | None = None) -> bool:
    """Test-only allow-list: exact host names, IP literals or CIDR networks."""
    allow = get_settings().crawler_allow_private_hosts
    if not allow:
        return False
    host = host.lower().strip(".")
    for entry in allow:
        e = entry.strip().lower()
        if not e:
            continue
        if e == host:
            return True
        if ip is not None:
            try:
                if ip in ipaddress.ip_network(e, strict=False):
                    return True
            except ValueError:
                continue
    return False


def _override_for(host: str) -> tuple[str, int] | None:
    overrides = get_settings().crawler_host_overrides
    if not overrides:
        return None
    target = overrides.get(host.lower().strip("."))
    if not target:
        return None
    ip, _, port = target.rpartition(":")
    if not ip:
        raise SSRFBlocked(f"invalid crawler_host_overrides entry for {host!r}: {target!r}")
    return ip.strip("[]"), int(port)


def validate_url(url: str) -> None:
    """Pre-flight URL policy. Raises :class:`SSRFBlocked` when the URL must not be fetched."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise SSRFBlocked(f"malformed URL: {url!r}") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFBlocked(f"scheme not allowed: {scheme or '(none)'}")
    host = (parts.hostname or "").lower().strip(".")
    if not host:
        raise SSRFBlocked("URL has no host")
    if parts.username is not None or parts.password is not None:
        raise SSRFBlocked("credentials in URLs are not allowed")
    test_host = _override_for(host) is not None or _host_allowed_private(host, _parse_ip(host))
    if port is not None and port not in ALLOWED_PORTS and not test_host:
        raise SSRFBlocked(f"port not allowed: {port}")
    ip = _parse_ip(host)
    if ip is not None:
        if not is_public_ip(ip) and not test_host:
            raise SSRFBlocked(f"non-public IP address: {host}")
        return
    if not test_host and (host in _INTERNAL_HOSTS or host.endswith(_INTERNAL_SUFFIXES) or "." not in host):
        raise SSRFBlocked(f"internal host name: {host}")


async def _getaddrinfo(host: str, port: int) -> list[tuple[str, int]]:
    """Resolve host → [(ip, port)] via the event loop resolver (monkeypatched in tests)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    out: list[tuple[str, int]] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0])
        if (ip, port) not in out:
            out.append((ip, port))
    return out


async def resolve_safe(host: str, port: int, *, dns_timeout: float | None = DNS_TIMEOUT_S) -> list[tuple[str, int]]:
    """Resolve and validate every address of ``host``. Raises SSRFBlocked / httpcore.ConnectError."""
    host = host.lower().strip(".")
    override = _override_for(host)
    if override is not None:
        return [override]
    literal = _parse_ip(host)
    if literal is not None:
        addrs = [(str(literal), port)]
    else:
        try:
            addrs = await asyncio.wait_for(_getaddrinfo(host, port), timeout=dns_timeout)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(f"DNS resolution timed out for {host}") from exc
        except (socket.gaierror, UnicodeError, OSError) as exc:
            raise httpcore.ConnectError(f"DNS resolution failed for {host}: {exc}") from exc
    if not addrs:
        raise httpcore.ConnectError(f"no address for {host}")
    for ip, _port in addrs:
        parsed = _parse_ip(ip)
        if parsed is None or (not is_public_ip(parsed) and not _host_allowed_private(host, parsed)):
            log.warning("ssrf_blocked_address", host=host, ip=ip)
            raise SSRFBlocked(f"{host} resolves to a non-public address ({ip})")
    return addrs


class SafeNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore backend that only ever opens sockets to validated public IPs (rebinding-safe)."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore backend interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addrs = await resolve_safe(host, port, dns_timeout=timeout or DNS_TIMEOUT_S)
        last_exc: Exception | None = None
        for ip, p in addrs:
            try:
                return await self._inner.connect_tcp(
                    ip, p, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore backend interface
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise SSRFBlocked("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class SafeAsyncTransport(httpx.AsyncHTTPTransport):
    """``httpx.AsyncHTTPTransport`` whose connection pool uses :class:`SafeNetworkBackend`.

    httpx does not expose ``network_backend``; we build the httpcore pool ourselves with the
    same options httpx would use. No proxy support on purpose (a proxy would bypass the checks).
    """

    def __init__(
        self,
        *,
        verify: bool = True,
        limits: httpx.Limits | None = None,
        http2: bool = False,
        retries: int = 0,
    ) -> None:
        limits = limits or httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0)
        super().__init__(verify=verify, limits=limits, http2=http2, retries=retries, trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=verify, trust_env=False),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=http2,
            retries=retries,
            network_backend=SafeNetworkBackend(),
        )
