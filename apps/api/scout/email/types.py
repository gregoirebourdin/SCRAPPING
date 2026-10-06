"""Email finder / verifier contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scout.db.enums import EmailDiscoveryMethod, EmailStatus, SmtpResult


@dataclass
class VerificationResult:
    address: str
    syntax_valid: bool
    mx_valid: bool | None
    smtp_result: SmtpResult
    catch_all: bool | None
    disposable: bool
    role_address: bool
    free_provider: bool
    verifier: str  # "aftership" | "builtin" | "fixture"
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int | None = None


@dataclass
class EmailCandidate:
    address: str
    method: EmailDiscoveryMethod
    pattern: str | None  # e.g. "{first}.{last}"
    pattern_confidence: float  # prior that this address is right before verification (0–1)
    source_url: str | None = None
    supporting_samples: int = 0  # named samples backing `pattern` at this domain (known/inferred)


@dataclass
class EmailAttempt:
    """One verified candidate inside the waterfall (kept for learning and audit)."""

    candidate: EmailCandidate
    verification: VerificationResult
    status: EmailStatus
    confidence: float


@dataclass
class EmailFinding:
    """Outcome of the waterfall for one person."""

    address: str | None
    status: EmailStatus
    overall_confidence: float  # 0–1
    method: EmailDiscoveryMethod | None
    pattern: str | None
    pattern_confidence: float | None
    verification: VerificationResult | None
    candidates_tried: list[str] = field(default_factory=list)
    reason: str | None = None  # when address is None or status not acceptable
    source_url: str | None = None
    attempts: list[EmailAttempt] = field(default_factory=list)
