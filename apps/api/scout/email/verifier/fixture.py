"""Deterministic verifier backed by a JSON manifest — tests and E2E only (refused in production).

Manifest shape (shared with the discovery fixtures)::

    {"companies": [...],
     "email": {"domains": {"agence-x.fr": {"mx": true, "catch_all": false, "smtp": true,
                                           "mailboxes": ["marie@agence-x.fr"]}}}}

A mailbox is deliverable iff listed; catch-all domains accept everything; unknown domains have
no MX; `"smtp": false` simulates an environment where SMTP probing is unavailable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scout.config import get_settings
from scout.db.enums import SmtpResult
from scout.email.lists import is_disposable_domain, is_free_provider, is_role_local_part
from scout.email.syntax import is_valid_syntax, normalize_address, normalize_domain, split_address
from scout.email.types import VerificationResult


class FixtureVerifier:
    name = "fixture"

    def __init__(self, manifest_path: str | Path | None = None, *, manifest: dict[str, Any] | None = None) -> None:
        settings = get_settings()
        if settings.is_production:
            raise RuntimeError("FixtureVerifier is a test backend and is forbidden in production")
        if manifest is None:
            path = manifest_path or settings.discovery_fixture_manifest
            if not path:
                raise RuntimeError("FixtureVerifier needs DISCOVERY_FIXTURE_MANIFEST")
            manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        domains = ((manifest or {}).get("email") or {}).get("domains") or {}
        self._domains: dict[str, dict[str, Any]] = {}
        for name, spec in domains.items():
            d = normalize_domain(name)
            if d:
                boxes = {normalize_address(b) for b in spec.get("mailboxes", [])}
                self._domains[d] = {**spec, "mailboxes": {b for b in boxes if b}}

    async def verify(self, address: str) -> VerificationResult:
        addr = normalize_address(address)
        if addr is None or not is_valid_syntax(addr):
            return VerificationResult(
                address=(address or "").strip().lower(), syntax_valid=False, mx_valid=None,
                smtp_result=SmtpResult.not_attempted, catch_all=None, disposable=False, role_address=False,
                free_provider=False, verifier=self.name, error="invalid_syntax", duration_ms=0,
            )
        local, domain = split_address(addr)
        res = VerificationResult(
            address=addr, syntax_valid=True, mx_valid=None, smtp_result=SmtpResult.not_attempted,
            catch_all=None, disposable=is_disposable_domain(domain), role_address=is_role_local_part(local),
            free_provider=is_free_provider(domain), verifier=self.name, duration_ms=0,
        )
        if res.disposable:
            return res
        spec = self._domains.get(domain)
        if spec is None or not spec.get("mx", True):
            res.mx_valid = False
            res.raw = {"fixture": "no_mx"}
            return res
        res.mx_valid = True
        if spec.get("smtp", True) is False:
            res.raw = {"fixture": "smtp_unavailable"}
            return res
        if spec.get("catch_all"):
            res.smtp_result, res.catch_all = SmtpResult.accepted, True
        else:
            res.catch_all = False
            res.smtp_result = SmtpResult.accepted if addr in spec["mailboxes"] else SmtpResult.rejected
        res.raw = {"fixture": res.smtp_result.value}
        return res

    async def is_catch_all(self, domain: str) -> bool | None:
        spec = self._domains.get(normalize_domain(domain) or "")
        if spec is None or not spec.get("mx", True) or spec.get("smtp", True) is False:
            return None
        return bool(spec.get("catch_all", False))
