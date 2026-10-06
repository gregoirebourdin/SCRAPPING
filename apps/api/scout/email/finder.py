"""Email finding waterfall for one person (PIPELINE.md §3.3 `email` + `verify` stages).

1. published addresses tied to the person (company domain first, then free providers);
2. known domain pattern; 3. pattern inferred from addresses observed at the domain;
4. ranked permutations. Candidates are verified in order and probing stops at the first SAFE,
after one probe on a catch-all domain, and as soon as SMTP turns out to be unavailable or
inconclusive (every remaining guess would get the same MX-only verdict). No AI, ever.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence

import structlog

from scout.db.enums import EmailDiscoveryMethod, EmailStatus, SmtpResult
from scout.email.lists import is_free_provider
from scout.email.patterns import infer_pattern, name_parts
from scout.email.permutations import rank_candidates
from scout.email.status import STATUS_RANK, derive_status
from scout.email.syntax import normalize_address, normalize_domain, split_address
from scout.email.types import EmailAttempt, EmailCandidate, EmailFinding, VerificationResult
from scout.email.verifier import get_verifier
from scout.email.verifier.base import EmailVerifier

log = structlog.get_logger(__name__)

DEFAULT_ACCEPT: frozenset[EmailStatus] = frozenset({EmailStatus.SAFE, EmailStatus.RISKY})
PUBLISHED_ON_DOMAIN_CONFIDENCE = 0.95
PUBLISHED_FREE_CONFIDENCE = 0.6
# Two-letter initials are too weak to tie a published address to a person.
_WEAK_PUBLISHED_PATTERNS = frozenset({"{f}{l}"})
_INCONCLUSIVE = (SmtpResult.not_attempted, SmtpResult.timeout, SmtpResult.blocked, SmtpResult.unknown)
_SEP = re.compile(r"[._+-]+")

REASON_NO_DOMAIN = "No company domain"
REASON_NO_CANDIDATE = "No email candidate"
REASON_NO_MX = "Domain has no MX record"
REASON_DISPOSABLE = "Disposable email domain"
REASON_REJECTED = "All email candidates were rejected by the mail server"
REASON_INVALID = "No valid email address"
REASON_INCONCLUSIVE = "Email verification inconclusive"
REASON_SMTP_UNAVAILABLE = "Email verification inconclusive (SMTP verification unavailable)"
REASON_CATCH_ALL = "Catch-all domain: the address cannot be confirmed"
REASON_RISKY = "Email is risky (not confirmed by the mail server)"


def matches_person(first: str | None, last: str | None, local_part: str) -> tuple[bool, str | None]:
    """Whether a published local part belongs to this person → (matched, vocabulary pattern)."""
    pattern = infer_pattern(first, last, local_part)
    if pattern is not None:
        return pattern not in _WEAK_PUBLISHED_PATTERNS, pattern
    parts = name_parts(first, last)
    lp = local_part.lower().split("+", 1)[0]
    tokens = [t for t in _SEP.split(lp) if t]
    # Full first AND last name must appear ("dupont.marie.paris"); initials alone could be a relative.
    has_last = any(v in tokens or (len(v) >= 4 and v.replace("-", "") in lp) for v in parts.last)
    has_first = any(v in tokens or (len(v) >= 3 and v.replace("-", "") in lp) for v in parts.first)
    return (has_last and has_first), None


def published_candidates(
    first: str | None, last: str | None, domain: str | None, published: Sequence[tuple[str, str | None]]
) -> list[EmailCandidate]:
    """Published addresses tied to the person: on the company domain first, then free providers."""
    on_domain: list[EmailCandidate] = []
    free: list[EmailCandidate] = []
    seen: set[str] = set()
    for raw, source_url in published:
        addr = normalize_address(raw)
        if addr is None or addr in seen:
            continue
        local, d = split_address(addr)
        is_own = domain is not None and (d == domain or d.endswith("." + domain))
        if not is_own and not is_free_provider(d):
            continue
        matched, pattern = matches_person(first, last, local)
        if not matched:
            continue
        seen.add(addr)
        cand = EmailCandidate(
            address=addr,
            method=EmailDiscoveryMethod.published,
            pattern=pattern,
            pattern_confidence=PUBLISHED_ON_DOMAIN_CONFIDENCE if is_own else PUBLISHED_FREE_CONFIDENCE,
            source_url=source_url,
        )
        (on_domain if is_own else free).append(cand)
    return on_domain + free


def _failure_reason(attempts: list[EmailAttempt]) -> str:
    vs = [a.verification for a in attempts]
    if any(v.mx_valid is False for v in vs):
        return REASON_NO_MX
    if any(v.disposable for v in vs):
        return REASON_DISPOSABLE
    if all(v.smtp_result == SmtpResult.rejected for v in vs):
        return REASON_REJECTED
    return REASON_INVALID


def _reason_for(status: EmailStatus, v: VerificationResult) -> str:
    if status == EmailStatus.CATCH_ALL:
        return REASON_CATCH_ALL
    if status == EmailStatus.RISKY:
        return REASON_RISKY
    if status == EmailStatus.UNKNOWN and v.smtp_result == SmtpResult.not_attempted:
        return REASON_SMTP_UNAVAILABLE
    return REASON_INCONCLUSIVE


async def find_email(
    *,
    first: str | None,
    last: str | None,
    domain: str | None,
    published: Sequence[tuple[str, str | None]] = (),
    known_patterns: Sequence[tuple[str, float, int]] = (),
    observed_local_parts: Sequence[str] = (),
    observed_samples: Sequence[tuple[str, str, str]] = (),
    company_size_max: int | None = None,
    country: str | None = None,
    verifier: EmailVerifier | None = None,
    accept: Collection[EmailStatus] | None = None,
    max_probes: int = 6,
) -> EmailFinding:
    """Run the waterfall and return the best evidence-backed address (or why there is none)."""
    verifier = verifier or get_verifier()
    accepted_statuses = frozenset(accept) if accept else DEFAULT_ACCEPT
    dom = normalize_domain(domain)

    queue = published_candidates(first, last, dom, published)
    if dom is not None:
        seen = {c.address for c in queue}
        queue += [
            c
            for c in rank_candidates(
                first, last, dom,
                known_patterns=known_patterns,
                observed_local_parts=observed_local_parts,
                observed_samples=observed_samples,
                company_size_max=company_size_max,
                country=country,
                max_candidates=max_probes,
            )
            if c.address not in seen
        ]
    if not queue or max_probes <= 0:
        reason = REASON_NO_DOMAIN if dom is None and not queue else REASON_NO_CANDIDATE
        return EmailFinding(None, EmailStatus.UNKNOWN, 0.0, None, None, None, None, reason=reason)

    attempts: list[EmailAttempt] = []
    domain_closed = False  # no further guesses can be informative
    for cand in queue:
        if len(attempts) >= max_probes:
            break
        is_guess = cand.method != EmailDiscoveryMethod.published
        if is_guess and domain_closed:
            break
        v = await verifier.verify(cand.address)
        status, conf = derive_status(cand, v)
        attempts.append(EmailAttempt(cand, v, status, conf))
        if status == EmailStatus.SAFE:
            break
        if dom is None or split_address(cand.address)[1] != dom:
            continue
        if (
            v.mx_valid is False
            or v.disposable
            or v.catch_all is True
            or (v.syntax_valid and v.smtp_result in _INCONCLUSIVE)
        ):
            domain_closed = True

    best = max(
        enumerate(attempts),
        key=lambda ia: (STATUS_RANK[ia[1].status], ia[1].confidence, -ia[0]),
    )[1]
    tried = [a.candidate.address for a in attempts]
    if best.status == EmailStatus.INVALID:
        return EmailFinding(
            None, EmailStatus.INVALID, 0.0, None, None, None, best.verification,
            candidates_tried=tried, reason=_failure_reason(attempts), attempts=attempts,
        )
    c = best.candidate
    finding = EmailFinding(
        address=c.address,
        status=best.status,
        overall_confidence=best.confidence,
        method=c.method,
        pattern=c.pattern,
        pattern_confidence=c.pattern_confidence,
        verification=best.verification,
        candidates_tried=tried,
        reason=None if best.status in accepted_statuses else _reason_for(best.status, best.verification),
        source_url=c.source_url,
        attempts=attempts,
    )
    log.debug("email.find", domain=dom, address=c.address, status=best.status, tried=len(tried))
    return finding
