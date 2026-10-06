"""HTTP client for the Go email-verifier service (AfterShip/email-verifier, Railway service B).

Any service failure (network, HTTP status, malformed JSON) falls back to the builtin verifier
for that call: the pipeline never crashes because Service B is down.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from scout.config import get_settings
from scout.db.enums import SmtpResult, UsageCategory
from scout.email import dns
from scout.email.lists import is_disposable_domain, is_free_provider, is_role_local_part
from scout.email.syntax import normalize_address, normalize_domain, split_address
from scout.email.types import VerificationResult
from scout.email.verifier.base import EmailVerifier
from scout.services.usage import record_usage

log = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_S = 45.0


def _opt_bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def map_smtp(smtp: dict[str, Any], *, syntax_valid: bool) -> tuple[SmtpResult, bool | None]:
    """AfterShip SMTP block → (smtp_result, catch_all)."""
    if not syntax_valid or not smtp.get("enabled"):
        return SmtpResult.not_attempted, None
    err = smtp.get("error")
    if err:
        low = str(err).lower()
        if "time" in low:
            return SmtpResult.timeout, None
        return (SmtpResult.unknown if smtp.get("host_exists") else SmtpResult.blocked), None
    catch_all = _opt_bool(smtp.get("catch_all"))
    if smtp.get("deliverable"):
        return SmtpResult.accepted, catch_all
    if catch_all:
        return SmtpResult.unknown, True  # catch-all: the mailbox itself is not checked by AfterShip
    if smtp.get("full_inbox") or smtp.get("disabled"):
        return SmtpResult.unknown, catch_all
    if smtp.get("host_exists"):
        return SmtpResult.rejected, catch_all
    return SmtpResult.unknown, catch_all


class ServiceVerifier:
    """Verifies through `POST {base_url}/v1/verify` and `/v1/catch-all` (bearer token auth)."""

    name = "aftership"

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        fallback: EmailVerifier | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        use_db_cache: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = timeout
        self._transport = transport
        self._fallback = fallback
        self.use_db_cache = use_db_cache

    def _fallback_verifier(self) -> EmailVerifier:
        if self._fallback is None:
            from scout.email.verifier.builtin import BuiltinVerifier

            self._fallback = BuiltinVerifier(use_db_cache=self.use_db_cache)
        return self._fallback

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._headers, timeout=self._timeout, transport=self._transport
        ) as client:
            resp = await client.post(path, json=payload)
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("unexpected verifier response")
        return data

    async def verify(self, address: str) -> VerificationResult:
        t0 = time.monotonic()
        addr = normalize_address(address) or (address or "").strip().lower()
        try:
            data = await self._post("/v1/verify", {"email": addr})
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("email.verifier_service.failed", error=str(exc), fallback="builtin")
            res = await self._fallback_verifier().verify(address)
            res.raw = {**res.raw, "service_error": str(exc)[:300]}
            return res
        try:
            await record_usage(
                UsageCategory.verification_request, cost_usd=get_settings().cost_verification_usd
            )
        except Exception as exc:  # usage accounting must never break verification
            log.warning("email.usage_record_failed", error=str(exc))
        res = self.map_result(addr, data)
        if res.syntax_valid and res.mx_valid is False and not res.disposable:
            # AfterShip only reads MX records; an A record is an implicit MX (RFC 5321).
            info = await dns.mx_lookup(split_address(addr)[1], use_cache=self.use_db_cache)
            if info.accepts_mail:
                res.mx_valid = True
        res.duration_ms = int((time.monotonic() - t0) * 1000)
        return res

    def map_result(self, addr: str, data: dict[str, Any]) -> VerificationResult:
        """Service JSON → VerificationResult (local lists complement AfterShip's flags)."""
        local, _, domain = addr.rpartition("@")
        syntax_valid = bool(data.get("syntax_valid"))
        disposable = bool(data.get("disposable")) or (bool(domain) and is_disposable_domain(domain))
        raw_smtp = data.get("smtp")
        smtp: dict[str, Any] = raw_smtp if isinstance(raw_smtp, dict) else {}
        smtp_result, catch_all = map_smtp(smtp, syntax_valid=syntax_valid)
        mx_valid: bool | None = None
        if syntax_valid and not disposable:
            mx_valid = bool(data.get("has_mx")) if "has_mx" in data else None
            if (
                data.get("error")
                and not data.get("has_mx")
                and "no such host" not in str(data["error"]).lower()
            ):
                mx_valid = None  # DNS trouble, not a definitive "no MX"
        return VerificationResult(
            address=addr,
            syntax_valid=syntax_valid,
            mx_valid=mx_valid,
            smtp_result=smtp_result,
            catch_all=catch_all,
            disposable=disposable,
            role_address=bool(data.get("role_account")) or (bool(local) and is_role_local_part(local)),
            free_provider=bool(data.get("free")) or (bool(domain) and is_free_provider(domain)),
            verifier=self.name,
            raw=data,
            error=(str(data["error"]) if data.get("error") else None)
            or (str(smtp["error"]) if smtp.get("error") else None),
        )

    async def is_catch_all(self, domain: str) -> bool | None:
        d = normalize_domain(domain)
        if d is None:
            return None
        if self.use_db_cache and (cached := await dns.cached_catch_all(d)) is not None:
            return cached
        try:
            data = await self._post("/v1/catch-all", {"domain": d})
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("email.verifier_service.catch_all_failed", error=str(exc), fallback="builtin")
            return await self._fallback_verifier().is_catch_all(d)
        value = _opt_bool(data.get("catch_all"))
        if self.use_db_cache:
            await dns.store_catch_all(d, value)
        return value
