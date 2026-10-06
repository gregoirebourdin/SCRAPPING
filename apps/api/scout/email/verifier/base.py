"""Verifier contract shared by the builtin, service (Go/AfterShip) and fixture backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from scout.email.types import VerificationResult


@runtime_checkable
class EmailVerifier(Protocol):
    name: str

    async def verify(self, address: str) -> VerificationResult:
        """Raw deliverability signals for one address. Must never raise for bad input."""
        ...

    async def is_catch_all(self, domain: str) -> bool | None:
        """True/False when the domain's mail server was probed (or cached); None when unknown."""
        ...
