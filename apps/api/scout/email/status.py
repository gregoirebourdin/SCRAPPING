"""Status derivation: raw verifier signals + candidate provenance → (EmailStatus, confidence).

Rules, evaluated in order (first match wins); confidence is the 0–1 probability that the
address reaches this person:

1. ``INVALID`` — bad syntax, disposable domain, no MX and no A record, or SMTP rejected (0.0–0.02).
2. Person addresses only (``for_person=True``):
   a. free-provider address (gmail, orange.fr…) → ``RISKY`` ≤ 0.5;
   b. role address not derived from the person's name (contact@, info@…) → ``RISKY`` ≤ 0.45.
3. ``SAFE`` 0.95 — SMTP accepted on a domain proven *not* catch-all (and not disposable).
4. ``SAFE`` 0.92 — published on the company's own website for this person, MX valid, SMTP not
   rejected and the domain not known as catch-all.
5. Catch-all domain (never ``SAFE`` for a guessed address):
   published → ``RISKY`` 0.8; strong pattern (≥ 0.85 from ≥ 2 samples) → ``RISKY`` 0.6–0.8;
   otherwise ``CATCH_ALL`` 0.35–0.55 scaled by pattern confidence.
6. SMTP accepted but catch-all state unknown → ``RISKY`` 0.7–0.8.
7. SMTP not attempted / timeout / blocked / unknown (or DNS inconclusive): published or strong
   known/inferred pattern → ``RISKY`` 0.6–0.75; otherwise ``UNKNOWN`` 0.3–0.5.
"""

from __future__ import annotations

from scout.db.enums import EmailDiscoveryMethod, EmailStatus, SmtpResult
from scout.email.types import EmailCandidate, VerificationResult

STRONG_PATTERN = 0.85
STRONG_SAMPLES = 2
_PATTERN_METHODS = (EmailDiscoveryMethod.known_pattern, EmailDiscoveryMethod.inferred_pattern)

# Ordering used to pick the best finding (higher is better).
STATUS_RANK: dict[EmailStatus, int] = {
    EmailStatus.INVALID: 0,
    EmailStatus.UNKNOWN: 1,
    EmailStatus.TEMPORARY_UNKNOWN: 2,
    EmailStatus.CATCH_ALL: 3,
    EmailStatus.RISKY: 4,
    EmailStatus.LIKELY_SAFE: 5,
    EmailStatus.SAFE: 6,
}


def is_strong_pattern(c: EmailCandidate) -> bool:
    """Known/inferred domain pattern backed by enough named evidence."""
    return (
        c.method in _PATTERN_METHODS
        and c.pattern_confidence >= STRONG_PATTERN
        and c.supporting_samples >= STRONG_SAMPLES
    )


def _strength(pc: float, lo: float, hi: float) -> float:
    """Linear map of pattern confidence in [0.85, 0.97] onto [lo, hi]."""
    t = min(1.0, max(0.0, (pc - STRONG_PATTERN) / 0.12))
    return lo + (hi - lo) * t


def derive_status(
    candidate: EmailCandidate, v: VerificationResult, *, for_person: bool = True
) -> tuple[EmailStatus, float]:
    """Status and overall confidence (0–1) for a candidate given its verification (see module doc)."""
    pc = min(1.0, max(0.0, candidate.pattern_confidence or 0.0))
    published = candidate.method == EmailDiscoveryMethod.published
    accepted = v.smtp_result == SmtpResult.accepted

    # 1. definitive failures
    if not v.syntax_valid or v.disposable or v.mx_valid is False:
        return EmailStatus.INVALID, 0.0
    if v.smtp_result == SmtpResult.rejected:
        return EmailStatus.INVALID, 0.02

    # 2. not a professional, personal mailbox
    if for_person and v.free_provider:
        if (accepted and v.catch_all is False) or published:
            return EmailStatus.RISKY, 0.5
        return EmailStatus.RISKY, round(0.3 + 0.15 * pc, 3)
    if for_person and v.role_address and candidate.pattern is None:
        return EmailStatus.RISKY, 0.45 if (accepted or published) else 0.3

    # 3–4. SAFE
    if accepted and v.catch_all is False:
        return EmailStatus.SAFE, 0.95
    if published and v.mx_valid is True and v.catch_all is not True:
        return EmailStatus.SAFE, 0.92

    # 5. catch-all domain
    if v.catch_all is True:
        if published:
            return EmailStatus.RISKY, 0.8
        if is_strong_pattern(candidate):
            return EmailStatus.RISKY, round(_strength(pc, 0.6, 0.8), 3)
        return EmailStatus.CATCH_ALL, round(0.35 + 0.2 * pc, 3)

    # 6. accepted, catch-all unknown
    if accepted:
        return EmailStatus.RISKY, round(0.7 + 0.1 * pc, 3)

    # 7. SMTP unavailable / inconclusive
    if published:
        return EmailStatus.RISKY, 0.7
    if is_strong_pattern(candidate):
        return EmailStatus.RISKY, round(_strength(pc, 0.6, 0.75), 3)
    return EmailStatus.UNKNOWN, round(0.3 + 0.2 * pc, 3)
