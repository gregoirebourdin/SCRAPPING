"""Verifier backends and the process-wide selection (`settings.verifier_backend`).

* ``auto``    → ``service`` when ``VERIFIER_SERVICE_URL`` is set, else ``builtin``
* ``service`` → Go/AfterShip service (falls back to builtin per call on failure)
* ``builtin`` → in-process syntax/MX/(optional) SMTP verifier
* ``fixture`` → JSON manifest (tests/E2E; refused in production)
"""

from __future__ import annotations

import structlog

from scout.config import Settings, get_settings
from scout.email.verifier.base import EmailVerifier
from scout.email.verifier.builtin import BuiltinVerifier
from scout.email.verifier.fixture import FixtureVerifier
from scout.email.verifier.service import ServiceVerifier, shared_breaker

log = structlog.get_logger(__name__)

__all__ = [
    "BuiltinVerifier",
    "EmailVerifier",
    "FixtureVerifier",
    "ServiceVerifier",
    "build_verifier",
    "get_verifier",
    "set_verifier",
]

_verifier: EmailVerifier | None = None


def build_verifier(settings: Settings | None = None) -> EmailVerifier:
    """A new verifier for the configured backend."""
    s = settings or get_settings()
    backend = s.verifier_backend
    if backend == "auto":
        backend = "service" if s.verifier_service_url else "builtin"
    if backend == "fixture":
        return FixtureVerifier(s.discovery_fixture_manifest)
    if backend == "service":
        if not s.verifier_service_url:
            log.warning("email.verifier.service_url_missing", fallback="builtin")
            return BuiltinVerifier()
        token = s.verifier_service_token.get_secret_value() if s.verifier_service_token else None
        return ServiceVerifier(
            s.verifier_service_url,
            token,
            fallback=BuiltinVerifier(),
            breaker=shared_breaker(s.verifier_service_url),
        )
    return BuiltinVerifier()


def get_verifier() -> EmailVerifier:
    """Process-wide verifier (cached)."""
    global _verifier
    if _verifier is None:
        _verifier = build_verifier()
    return _verifier


def set_verifier(verifier: EmailVerifier | None) -> None:
    """Override (tests) or reset (None → rebuilt from settings on next use)."""
    global _verifier
    _verifier = verifier
