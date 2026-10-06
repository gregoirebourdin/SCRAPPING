"""Email Intelligence Engine — fast path / deep path orchestration (docs/EMAIL_ENGINE.md).

FAST PATH (inline, no SMTP):
    cache / global registry → observed emails for this person (website, GitHub, RDAP, imports)
    → learned domain pattern → 1–3 ranked candidates → name-affinity guard → MX/DNS → confidence.
    When the best verdict is SAFE or LIKELY_SAFE (or the question cannot be settled by SMTP:
    catch-all domain, no MX, SMTP unavailable) the result is final and returned immediately.

DEEP PATH (background, per-domain SMTP batches — scout.email.deep):
    ambiguous candidates are queued; one SMTP session per domain verifies every pending person's
    candidates plus random catch-all probes; temporary answers are retried with backoff; the
    verdict is then persisted here and deferred campaign deliveries resume.

Priority is absolute: observed evidence > learned pattern > generated candidate. No LLM ever
produces or guesses an address.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import sqlalchemy as sa
import structlog

from scout.config import get_settings
from scout.db.engine import session_scope
from scout.db.enums import (
    EmailDiscoveryMethod,
    EmailEvidenceSource,
    EmailResolutionPath,
    EmailStatus,
    SmtpHealthState,
    SmtpResult,
)
from scout.db.models import Company, Email, EmailResolution, EmailVerificationRequest, Person
from scout.email import stats
from scout.email.affinity import ATTRIBUTE_MIN, STRONG, Affinity, name_affinity
from scout.email.confidence import RESOLVER_PRIORS, STATUS_RANK, Evidence, assess
from scout.email.contracts import (
    SMTP_USABLE,
    DomainIntel,
    DomainProbeResult,
    ObservedEmail,
    SessionOutcome,
    Verdict,
)
from scout.email.lists import is_disposable_domain, is_free_provider
from scout.email.patterns import VARIANT_DECAY, infer_from_local_parts, name_parts, ranked_priors, render
from scout.email.syntax import is_valid_syntax, normalize_address, normalize_domain, split_address
from scout.email.types import EmailFinding, VerificationResult

log = structlog.get_logger(__name__)

MAX_CANDIDATES = 3
MAX_PATTERNS = 2
MIN_PATTERN_CONFIDENCE = 0.2
EMAIL_FRESH = timedelta(days=60)
FINAL_FAST: frozenset[EmailStatus] = frozenset({EmailStatus.SAFE, EmailStatus.LIKELY_SAFE})

_SOURCE_RESOLVER: dict[EmailEvidenceSource, str] = {
    EmailEvidenceSource.website: "published_website",
    EmailEvidenceSource.user: "user",
    EmailEvidenceSource.import_: "import",
    EmailEvidenceSource.smtp_verified: "smtp_verified",
    EmailEvidenceSource.github: "github",
    EmailEvidenceSource.search: "search",
    EmailEvidenceSource.rdap: "rdap",
}
_SOURCE_ORDER = list(_SOURCE_RESOLVER)
_PUBLISHED_BY_COMPANY = frozenset(
    {EmailEvidenceSource.website, EmailEvidenceSource.user, EmailEvidenceSource.import_}
)
# Sources whose addresses only teach the domain convention and are never offered as a contact address.
PATTERN_EVIDENCE_ONLY = frozenset({EmailEvidenceSource.github})

# --------------------------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------------------------


@dataclass
class Candidate:
    address: str
    resolver: str
    method: EmailDiscoveryMethod
    base_probability: float
    base_detail: str
    affinity: Affinity
    pattern: str | None = None
    source_url: str | None = None
    published_on_domain: bool = False
    supporting_samples: int = 0
    round: int = 1  # 2: expansion round (next patterns after every first guess was rejected)

    def as_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "resolver": self.resolver,
            "method": self.method.value,
            "base_probability": self.base_probability,
            "base_detail": self.base_detail,
            "affinity": {
                "score": self.affinity.score,
                "pattern": self.affinity.pattern,
                "reason": self.affinity.reason,
            },
            "pattern": self.pattern,
            "source_url": self.source_url,
            "published_on_domain": self.published_on_domain,
            "supporting_samples": self.supporting_samples,
            "round": self.round,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Candidate:
        a = d.get("affinity") or {}
        return cls(
            address=d["address"],
            resolver=d.get("resolver", "permutation"),
            method=EmailDiscoveryMethod(d.get("method", "permutation")),
            base_probability=float(d.get("base_probability", 0.3)),
            base_detail=d.get("base_detail", ""),
            affinity=Affinity(float(a.get("score", 0.0)), a.get("pattern"), a.get("reason", "")),
            pattern=d.get("pattern"),
            source_url=d.get("source_url"),
            published_on_domain=bool(d.get("published_on_domain")),
            supporting_samples=int(d.get("supporting_samples", 0)),
            round=int(d.get("round", 1)),
        )


def _same_person(o: ObservedEmail, first: str | None, last: str | None) -> bool:
    if not (o.first_name and o.last_name):
        return True  # nameless observation: affinity decides
    a, b = name_parts(o.first_name, o.last_name), name_parts(first, last)
    return bool(set(a.first) & set(b.first)) and bool(set(a.last) & set(b.last))


def build_candidates(
    first: str | None,
    last: str | None,
    intel: DomainIntel,
    *,
    colleagues: Sequence[tuple[str | None, str | None]] = (),
    extra_observed: Sequence[ObservedEmail] = (),
    company_size_max: int | None = None,
    country: str | None = None,
    max_candidates: int = MAX_CANDIDATES,
    exhaustive: bool = False,
) -> list[Candidate]:
    """At most ``max_candidates`` ranked candidates: observed evidence, then learned pattern, then priors.

    ``exhaustive``: keep adding generic patterns even after a confirmed convention (expansion round).
    """
    dom = normalize_domain(intel.domain)
    if dom is None or name_parts(first, last).is_empty:
        return []
    dominant = intel.dominant
    dom_pattern, dom_share = (dominant.pattern, dominant.share) if dominant else (None, 0.0)

    def aff(local: str, *, context: bool = False) -> Affinity:
        return name_affinity(
            local,
            first,
            last,
            dominant_pattern=dom_pattern,
            dominant_share=dom_share,
            colleagues=colleagues,
            context_mentions_name=context,
        )

    out: list[Candidate] = []
    seen: set[str] = set()

    def push(c: Candidate) -> None:
        if c.address not in seen and len(out) < max_candidates:
            seen.add(c.address)
            out.append(c)

    # 1. observed for this person (published > imported > verified > GitHub > search > RDAP)
    observed = sorted(
        [*intel.observed, *extra_observed],
        key=lambda o: (_SOURCE_ORDER.index(o.source) if o.source in _SOURCE_ORDER else 99, -o.confidence),
    )
    for o in observed:
        if o.source in PATTERN_EVIDENCE_ONLY:
            continue  # e.g. GitHub commit emails: convention evidence, never a contact address (GitHub AUP)
        addr = normalize_address(o.address)
        if addr is None or o.is_role or not _same_person(o, first, last):
            continue
        local, d = split_address(addr)
        own = d == dom or d.endswith("." + dom)
        if not own and not is_free_provider(d):
            continue
        a = aff(local, context=bool(o.first_name and o.last_name))
        if a.score < STRONG:
            continue  # seen at the company, but not this person's address
        resolver = _SOURCE_RESOLVER.get(o.source, "search")
        push(
            Candidate(
                address=addr,
                resolver=resolver,
                method=EmailDiscoveryMethod.published
                if o.source != EmailEvidenceSource.import_
                else EmailDiscoveryMethod.import_,
                base_probability=RESOLVER_PRIORS.get(resolver, 0.7) * (1.0 if own else 0.65),
                base_detail=f"Observed address ({o.source.value.replace('_', ' ')})",
                affinity=a,
                pattern=a.pattern,
                source_url=o.source_url,
                published_on_domain=own and o.source in _PUBLISHED_BY_COMPANY,
            )
        )

    # 2. learned domain patterns (dominant first)
    learned = [p for p in intel.patterns if p.confidence >= MIN_PATTERN_CONFIDENCE][:MAX_PATTERNS]
    for ps in learned:
        for idx, local in enumerate(render(ps.pattern, first, last)[:2]):
            a = aff(local)
            if a.score < ATTRIBUTE_MIN:
                continue
            evidence = f"{ps.samples} real email{'s' if ps.samples != 1 else ''}" + (
                f", {ps.successes} SMTP-confirmed" if ps.successes else ""
            )
            push(
                Candidate(
                    address=f"{local}@{dom}",
                    resolver="domain_pattern",
                    method=EmailDiscoveryMethod.known_pattern,
                    base_probability=round(ps.confidence * VARIANT_DECAY**idx, 4),
                    base_detail=f"Domain convention {ps.pattern} = {round(ps.share * 100)}% ({evidence})",
                    affinity=a,
                    pattern=ps.pattern,
                    supporting_samples=ps.samples + ps.successes,
                )
            )
    if learned and learned[0].confidence >= 0.85 and not exhaustive:
        return out  # a confirmed convention: do not dilute with generic guesses

    # 3. nameless shapes observed at the domain, then context priors
    shapes = infer_from_local_parts(
        o.local_part for o in intel.observed if not o.is_role and not (o.first_name and o.last_name)
    )
    for pattern, conf, count in shapes[:1]:
        for local in render(pattern, first, last)[:1]:
            a = aff(local)
            if a.score >= ATTRIBUTE_MIN:
                push(
                    Candidate(
                        address=f"{local}@{dom}",
                        resolver="inferred_shape",
                        method=EmailDiscoveryMethod.inferred_pattern,
                        base_probability=conf,
                        base_detail=f"Shape of {count} address{'es' if count != 1 else ''} seen at the domain suggests {pattern}",
                        affinity=a,
                        pattern=pattern,
                    )
                )
    for pattern, prior in ranked_priors(company_size_max=company_size_max, country=country):
        if len(out) >= max_candidates:
            break
        for local in render(pattern, first, last)[:1]:
            a = aff(local)
            if a.score >= ATTRIBUTE_MIN:
                push(
                    Candidate(
                        address=f"{local}@{dom}",
                        resolver="permutation",
                        method=EmailDiscoveryMethod.permutation,
                        base_probability=prior,
                        base_detail=f"Common convention {pattern} (prior {round(prior * 100)}%)",
                        affinity=a,
                        pattern=pattern,
                    )
                )
    return out


# --------------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------------


def evaluate(
    candidates: Sequence[Candidate],
    intel: DomainIntel | None,
    *,
    probe: DomainProbeResult | None = None,
    smtp_state: SmtpHealthState = SmtpHealthState.UNKNOWN,
    retry_pending: bool = False,
    snap: dict[tuple[str, str], stats.StatRow] | None = None,
) -> list[Verdict]:
    catch_all = (
        probe.catch_all
        if probe is not None and probe.catch_all is not None
        else (intel.catch_all if intel else None)
    )
    out: list[Verdict] = []
    for c in candidates:
        _, d = split_address(c.address)
        rv = probe.verdicts.get(c.address) if probe is not None else None
        ev = Evidence(
            address=c.address,
            resolver=c.resolver,
            base_probability=c.base_probability,
            base_detail=c.base_detail,
            affinity=c.affinity,
            published_on_domain=c.published_on_domain,
            source_url=c.source_url,
            has_mx=intel.has_mx if intel else None,
            accepts_mail=intel.accepts_mail if intel else None,
            catch_all=catch_all,
            catch_all_confidence=(probe.catch_all_confidence if probe else None)
            or (intel.catch_all_confidence if intel else None),
            smtp=rv.result if rv is not None else None,
            smtp_state=smtp_state,
            smtp_detail=f"{rv.code} {rv.message}".strip() if rv is not None and rv.code else None,
            free_provider=is_free_provider(d),
            disposable=is_disposable_domain(d),
            syntax_valid=is_valid_syntax(c.address),
            retry_pending=retry_pending,
        )
        out.append(assess(ev, snap))
    return out


def pick(verdicts: Sequence[Verdict]) -> Verdict | None:
    """Best attributable verdict (affinity-guarded candidates never win)."""
    usable = [v for v in verdicts if not any(s.name == "affinity_guard" for s in v.signals)]
    if not usable:
        return None
    return max(enumerate(usable), key=lambda iv: (STATUS_RANK[iv[1].status], iv[1].confidence, -iv[0]))[1]


DeepMode = Literal["none", "decide", "confirm"]
# A learned pattern backed by fewer real addresses / SMTP successes than this is confirmed in the background.
CONFIRM_BELOW_EVIDENCE = 3


def _smtp_capable() -> bool:
    s = get_settings()
    return s.smtp_enabled or bool(s.verifier_service_url) or s.verifier_backend == "fixture"


def deep_plan(
    best: Verdict | None,
    intel: DomainIntel | None,
    smtp_state: SmtpHealthState,
    *,
    smtp_capable: bool | None = None,
) -> DeepMode:
    """What the deep (SMTP) path should do for this fast-path verdict.

    ``decide``: ambiguous — SMTP settles it and campaign delivery waits for the verdict.
    ``confirm``: already acceptable (LIKELY_SAFE) but resting on a thin learned pattern — delivered now, confirmed
    in the background (a success upgrades it to SAFE and teaches the domain; a rejection corrects it).
    ``none``: final, SMTP cannot help (catch-all, no MX, SMTP unavailable/blocked) or not worth a probe.
    """
    if not (_smtp_capable() if smtp_capable is None else smtp_capable) or smtp_state not in SMTP_USABLE:
        return "none"
    if best is None or best.status in (EmailStatus.SAFE, EmailStatus.INVALID):
        return "none"
    if intel is not None and (intel.catch_all is True or intel.accepts_mail is False):
        return "none"
    if best.status == EmailStatus.LIKELY_SAFE:
        dom = intel.dominant if intel is not None else None
        thin = dom is None or dom.samples + dom.successes < CONFIRM_BELOW_EVIDENCE
        return "confirm" if best.resolver == "domain_pattern" and thin else "none"
    return "decide"


def deep_would_help(
    best: Verdict | None,
    intel: DomainIntel | None,
    smtp_state: SmtpHealthState,
    *,
    smtp_capable: bool | None = None,
) -> bool:
    """SMTP can only settle ambiguous candidates on a domain that is not catch-all, with a usable path."""
    return deep_plan(best, intel, smtp_state, smtp_capable=smtp_capable) == "decide"


LEAN_MIN_PROBABILITY = 0.6


def deep_candidates(
    usable: Sequence[Candidate], mode: DeepMode, best: Verdict | None = None
) -> list[Candidate]:
    """Fewest RCPT commands: a confirmation probes the chosen address only; a pattern-backed guess probes its
    best rendering first (the expansion round tries the next ones if it is rejected); otherwise ≤ 3 candidates."""
    if not usable:
        return []
    if mode == "confirm" and best is not None:
        chosen = [c for c in usable if c.address == best.address]
        if chosen:
            return chosen[:1]
    top = usable[0]
    if top.resolver == "domain_pattern" and top.base_probability >= LEAN_MIN_PROBABILITY:
        return [top]
    return list(usable[:MAX_CANDIDATES])


EXPANSION_ROUND = 2
_GUESS_RESOLVERS = frozenset({"permutation", "inferred_shape", "domain_pattern"})


def should_expand(
    candidates: Sequence[Candidate],
    intel: DomainIntel | None,
    probe: DomainProbeResult | None,
    smtp_state: SmtpHealthState,
) -> bool:
    """Every first guess was definitively rejected by a healthy, proven non catch-all server, and the domain
    has no confirmed convention: the next patterns deserve ONE more batched try (not for observed addresses —
    a rejected published address means the person left, not that we guessed wrong)."""
    if probe is None or probe.session != SessionOutcome.ok or probe.catch_all is not False:
        return False
    if smtp_state == SmtpHealthState.BLOCKED or not candidates:
        return False
    if any(c.round >= EXPANSION_ROUND or c.resolver not in _GUESS_RESOLVERS for c in candidates):
        return False
    dom = intel.dominant if intel is not None else None
    if dom is not None and dom.confidence >= 0.85 and dom.samples + dom.successes >= 3:
        return False  # a proven convention was rejected: this person most likely has no mailbox
    return all(
        (rv := probe.verdicts.get(c.address)) is not None and rv.result == SmtpResult.rejected
        for c in candidates
    )


def expand_candidates(
    first: str | None,
    last: str | None,
    intel: DomainIntel,
    tried: Sequence[str],
    *,
    colleagues: Sequence[tuple[str | None, str | None]] = (),
    company_size_max: int | None = None,
    country: str | None = None,
    max_new: int = MAX_CANDIDATES,
) -> list[Candidate]:
    """The next ``max_new`` ranked candidates not tried yet (expansion round)."""
    seen = set(tried)
    pool = build_candidates(
        first,
        last,
        intel,
        colleagues=colleagues,
        company_size_max=company_size_max,
        country=country,
        max_candidates=len(seen) + max_new + MAX_CANDIDATES,
        exhaustive=True,
    )
    fresh = [c for c in pool if c.address not in seen and c.resolver in _GUESS_RESOLVERS]
    for c in fresh:
        c.round = EXPANSION_ROUND
    return fresh[:max_new]


PILOT_MAX_CONVENTION = 0.5  # below this, the domain's convention is treated as unknown


def pick_pilot(batch: Sequence[Sequence[Candidate]], intel: DomainIntel | None) -> int | None:
    """Index of the person to probe first when a domain batch would otherwise send every person's ≤ 3 guesses.

    Only when the convention is unknown and at least two people carry several pattern guesses: the pilot's
    answer tells which pattern the domain uses, and the others then need a single RCPT each.
    """
    dom = intel.dominant if intel is not None else None
    if dom is not None and dom.confidence >= PILOT_MAX_CONVENTION:
        return None
    if intel is not None and intel.catch_all is True:
        return None
    multi = [
        i
        for i, cands in enumerate(batch)
        if len(cands) >= 2 and all(c.resolver in _GUESS_RESOLVERS and c.pattern for c in cands)
    ]
    if len(multi) < 2:
        return None
    return max(multi, key=lambda i: (len(batch[i]), -i))


def narrow_after_pilot(
    pilot: Sequence[Candidate], probe: DomainProbeResult, others: Sequence[Sequence[Candidate]]
) -> list[list[Candidate]] | None:
    """After the pilot's probe: each other person's candidates restricted to the confirmed pattern.

    None when the pilot did not identify the convention unambiguously (not exactly one accepted pattern on a
    proven non catch-all server); the caller then probes everyone's candidates as usual.
    """
    if probe.session != SessionOutcome.ok or probe.catch_all is not False:
        return None
    accepted = {
        c.pattern
        for c in pilot
        if c.pattern
        and (rv := probe.verdicts.get(c.address)) is not None
        and rv.result == SmtpResult.accepted
    }
    if len(accepted) != 1:
        return None
    pattern = next(iter(accepted))
    out: list[list[Candidate]] = []
    for cands in others:
        same = [c for c in cands if c.pattern == pattern]
        # the pattern was just confirmed on this server: a rejection means "no mailbox", never "expand"
        out.append([replace(same[0], round=EXPANSION_ROUND)] if same else list(cands))
    return out


def conclude_after_probe(
    candidates: list[dict[str, Any]],
    intel: DomainIntel | None,
    probe: DomainProbeResult | None,
    *,
    smtp_state: SmtpHealthState,
    attempt: int,
    max_attempts: int,
    first_name: str | None = None,
    last_name: str | None = None,
    snap: dict[tuple[str, str], stats.StatRow] | None = None,
) -> tuple[Verdict | None, bool]:
    """Deep-path verdict for one person after the per-domain SMTP probe → (verdict, retry)."""
    cands = [Candidate.from_dict(d) for d in candidates]
    temporary = probe is not None and (
        probe.session.value == "temporary"
        or any(
            v.result in (SmtpResult.temporary, SmtpResult.timeout)
            for a, v in probe.verdicts.items()
            if a in {c.address for c in cands}
        )
    )
    retry = temporary and attempt < max_attempts
    verdicts = evaluate(cands, intel, probe=probe, smtp_state=smtp_state, retry_pending=retry, snap=snap)
    best = pick(verdicts)
    if best is not None and best.status == EmailStatus.SAFE:
        retry = False
    return best, retry


# --------------------------------------------------------------------------------------------
# Persistence, stats, hooks
# --------------------------------------------------------------------------------------------

ResolvedHook = Callable[[EmailVerificationRequest, Verdict | None], Awaitable[None]]
_resolved_hooks: list[ResolvedHook] = []


def on_email_resolved(fn: ResolvedHook) -> ResolvedHook:
    """Register a callback run after a deep-path verdict is persisted (deferred campaign delivery)."""
    if fn not in _resolved_hooks:
        _resolved_hooks.append(fn)
    return fn


def _finding(
    verdict: Verdict,
    cand: Candidate | None,
    intel: DomainIntel | None,
    probe: DomainProbeResult | None,
    path: str,
) -> EmailFinding:
    rv = probe.verdicts.get(verdict.address) if probe is not None else None
    _, d = split_address(verdict.address)
    v = VerificationResult(
        address=verdict.address,
        syntax_valid=is_valid_syntax(verdict.address),
        mx_valid=intel.has_mx if intel else None,
        smtp_result=rv.result if rv is not None else SmtpResult.not_attempted,
        catch_all=(
            probe.catch_all
            if probe is not None and probe.catch_all is not None
            else (intel.catch_all if intel else None)
        ),
        disposable=is_disposable_domain(d),
        role_address=False,
        free_provider=is_free_provider(d),
        verifier=(probe.verifier if probe is not None else "engine"),
        raw={
            "path": path,
            "resolver": verdict.resolver,
            "affinity": verdict.affinity,
            "signals": verdict.explanation,
        },
        duration_ms=probe.duration_ms if probe is not None else None,
    )
    return EmailFinding(
        address=verdict.address,
        status=verdict.status,
        overall_confidence=verdict.confidence,
        method=cand.method if cand else EmailDiscoveryMethod.permutation,
        pattern=cand.pattern if cand else None,
        pattern_confidence=cand.base_probability if cand else None,
        verification=v,
        source_url=cand.source_url if cand else None,
    )


def _technique(path: EmailResolutionPath, cand: Candidate | None) -> str:
    if path == EmailResolutionPath.deep:
        return "smtp_rcpt"
    if cand is not None and cand.resolver in (
        "published_website",
        "import",
        "user",
        "github",
        "rdap",
        "search",
        "smtp_verified",
    ):
        return "observed"
    return "mx_only"


def _stat_events(
    cand: Candidate | None,
    intel: DomainIntel | None,
    path: EmailResolutionPath,
    *,
    correct: bool | None,
    outcome: bool,
    latency_ms: int = 0,
) -> list[stats.StatEvent]:
    if cand is None:
        return []
    keys = [("resolver", cand.resolver), ("technique", _technique(path, cand))]
    if cand.pattern:
        keys.append(("pattern", cand.pattern))
    if intel is not None:
        keys.append(("provider", intel.provider.value))
    return [
        stats.StatEvent(
            dim, key, correct=correct, outcome=outcome, latency_ms=latency_ms if not outcome else 0
        )
        for dim, key in keys
    ]


async def _log_resolution(
    workspace_id: uuid.UUID,
    *,
    person_id: uuid.UUID | None,
    campaign_id: uuid.UUID | None,
    domain: str | None,
    path: EmailResolutionPath,
    verdict: Verdict | None,
    candidates: int,
    smtp_probes: int,
    cache_hits: dict[str, Any],
    duration_ms: int,
    status: EmailStatus | None = None,
) -> None:
    async with session_scope() as s:
        s.add(
            EmailResolution(
                workspace_id=workspace_id,
                person_id=person_id,
                campaign_id=campaign_id,
                domain=domain,
                path=path,
                status=verdict.status if verdict else (status or EmailStatus.UNKNOWN),
                resolver=verdict.resolver if verdict else None,
                address=verdict.address if verdict else None,
                confidence=verdict.confidence if verdict else None,
                candidates_considered=candidates,
                smtp_probes=smtp_probes,
                cache_hits=cache_hits,
                duration_ms=duration_ms,
                cost_usd=Decimal("0"),
                explanation=verdict.explanation if verdict else [],
            )
        )


async def _learn_from_probe(
    domain: str,
    cands: Sequence[Candidate],
    probe: DomainProbeResult,
    smtp_state: SmtpHealthState,
    first: str | None,
    last: str | None,
    workspace_id: uuid.UUID,
) -> None:
    """Healthy, non catch-all SMTP answers teach the domain memory (patterns + verified samples)."""
    if probe.catch_all is not False or smtp_state == SmtpHealthState.BLOCKED:
        return
    from scout.email.store import record_pattern_outcome

    verified: list[ObservedEmail] = []
    for c in cands:
        rv = probe.verdicts.get(c.address)
        if rv is None:
            continue
        if rv.result == SmtpResult.accepted:
            if c.pattern:
                await record_pattern_outcome(domain, c.pattern, success=True)
            local, _ = split_address(c.address)
            verified.append(
                ObservedEmail(
                    address=c.address,
                    local_part=local,
                    source=EmailEvidenceSource.smtp_verified,
                    first_name=first,
                    last_name=last,
                    pattern=c.pattern,
                    confidence=0.95,
                )
            )
        elif rv.result == SmtpResult.rejected and c.pattern:
            await record_pattern_outcome(domain, c.pattern, success=False)
    if verified:
        try:
            from scout.email.intel.samples import record_observed_emails

            await record_observed_emails(domain, verified, workspace_id=workspace_id)
        except ImportError:  # pragma: no cover - intel layer always present in production
            pass


async def persist_deep_verdict(
    request: EmailVerificationRequest,
    verdict: Verdict | None,
    probe: DomainProbeResult | None,
    *,
    smtp_state: SmtpHealthState,
    intel: DomainIntel | None = None,
) -> None:
    """Save the deep-path verdict, learn from SMTP answers, record stats, run deferred-delivery hooks."""
    from scout.email.store import save_finding

    cands = [Candidate.from_dict(d) for d in request.candidates]
    by_addr = {c.address: c for c in cands}
    async with session_scope() as s:
        person = await s.get(Person, request.person_id)
    first, last = _names(person) if person is not None else (None, None)

    if probe is not None:
        await _learn_from_probe(request.domain, cands, probe, smtp_state, first, last, request.workspace_id)
        events: list[stats.StatEvent] = []
        for c in cands:
            rv = probe.verdicts.get(c.address)
            if rv is None:
                continue
            proven = probe.catch_all is False and smtp_state != SmtpHealthState.BLOCKED
            if rv.result == SmtpResult.accepted and proven:
                events += _stat_events(c, intel, EmailResolutionPath.deep, correct=True, outcome=True)
            elif rv.result == SmtpResult.rejected and probe.catch_all is not True:
                events += _stat_events(c, intel, EmailResolutionPath.deep, correct=False, outcome=True)
        await stats.record(events)

    provisional = request.provisional_status
    if (
        request.deliver_context is None  # background confirmation of an already acceptable verdict
        and provisional is not None
        and provisional in FINAL_FAST
        and verdict is not None
        and verdict.status not in (EmailStatus.SAFE, EmailStatus.INVALID)
        and STATUS_RANK[verdict.status] < STATUS_RANK[provisional]
    ):
        verdict = None  # inconclusive background confirmation: keep the fast-path verdict as is
    if verdict is not None and person is not None:
        cand = by_addr.get(verdict.address)
        await save_finding(
            request.workspace_id,
            person_id=request.person_id,
            company_id=request.company_id,
            finding=_finding(verdict, cand, intel, probe, "deep"),
            make_primary=verdict.status != EmailStatus.INVALID,
        )
    await _log_resolution(
        request.workspace_id,
        person_id=request.person_id,
        campaign_id=request.campaign_id,
        domain=request.domain,
        path=EmailResolutionPath.deep,
        verdict=verdict,
        candidates=len(cands),
        smtp_probes=probe.probes if probe is not None else 0,
        cache_hits={},
        duration_ms=probe.duration_ms if probe is not None else 0,
    )
    for hook in list(_resolved_hooks):
        try:
            await hook(request, verdict)
        except Exception as exc:  # a delivery problem must not lose the verdict
            log.warning("email.resolved_hook_failed", error=str(exc), request_id=str(request.id))


# --------------------------------------------------------------------------------------------
# Fast path entry point
# --------------------------------------------------------------------------------------------


@dataclass
class Resolution:
    status: EmailStatus
    address: str | None
    confidence: float
    path: EmailResolutionPath
    verdict: Verdict | None = None
    candidates: list[Candidate] = field(default_factory=list)
    deep_requested: bool = False  # ambiguous: the deep path decides (campaign delivery waits)
    confirming: bool = False  # acceptable now, SMTP confirmation running in the background
    reason: str | None = None
    cache_hits: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def pending(self) -> bool:
        return self.deep_requested


def _names(p: Person) -> tuple[str | None, str | None]:
    first, last = p.first_name, p.last_name
    if first and last:
        return first, last
    tokens = (p.full_name or "").split()
    if len(tokens) >= 2:
        return first or tokens[0], last or " ".join(tokens[1:])
    return first or (tokens[0] if tokens else None), last


async def _smtp_state(provider: Any) -> SmtpHealthState:
    try:
        from scout.email.smtp.health import current_state
    except ImportError:  # pragma: no cover
        return SmtpHealthState.UNKNOWN
    return await current_state(provider)


async def _domain_intel(domain: str, **kw: Any) -> DomainIntel:
    from scout.email.intel.profile import get_domain_intel

    return await get_domain_intel(domain, **kw)


async def resolve_for_person(
    workspace_id: uuid.UUID,
    person_id: uuid.UUID,
    *,
    campaign_id: uuid.UUID | None = None,
    allow_deep: bool = True,
    deliver_context: dict[str, Any] | None = None,
    force: bool = False,
) -> Resolution:
    """Fast path for one person; queues the deep path only for ambiguous cases."""
    from scout.email.store import save_finding

    t0 = time.monotonic()
    async with session_scope() as s:
        person = await s.get(Person, person_id)
        if person is None or person.workspace_id != workspace_id:
            return Resolution(
                EmailStatus.UNKNOWN, None, 0.0, EmailResolutionPath.fast, reason="Person not found"
            )
        company = await s.get(Company, person.company_id) if person.company_id else None
        existing = (
            await s.execute(
                sa.select(Email)
                .where(Email.workspace_id == workspace_id, Email.person_id == person_id)
                .order_by(
                    Email.is_user_confirmed.desc(),
                    Email.is_primary.desc(),
                    Email.overall_confidence.desc().nullslast(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        colleagues = (
            [
                _names(p)
                for p in (
                    await s.scalars(
                        sa.select(Person).where(
                            Person.workspace_id == workspace_id,
                            Person.company_id == company.id,
                            Person.id != person_id,
                        )
                    )
                ).all()
            ]
            if company is not None
            else []
        )

    # ---- cache / global registry -----------------------------------------------------------
    if existing is not None and not force:
        fresh = (
            existing.last_checked_at is not None
            and datetime.now(UTC) - existing.last_checked_at < EMAIL_FRESH
        )
        if existing.is_user_confirmed or (fresh and existing.status in FINAL_FAST):
            ms = int((time.monotonic() - t0) * 1000)
            await _log_resolution(
                workspace_id,
                person_id=person_id,
                campaign_id=campaign_id,
                domain=existing.domain,
                path=EmailResolutionPath.cache,
                verdict=None,
                candidates=0,
                smtp_probes=0,
                cache_hits={"email": True},
                duration_ms=ms,
                status=existing.status,
            )
            return Resolution(
                existing.status,
                existing.address,
                existing.overall_confidence or 0.9,
                EmailResolutionPath.cache,
                cache_hits={"email": True},
                duration_ms=ms,
            )

    first, last = _names(person)
    domain = normalize_domain((company.normalized_domain or company.domain) if company else None)
    if domain is None:
        return Resolution(
            EmailStatus.UNKNOWN, None, 0.0, EmailResolutionPath.fast, reason="No company domain"
        )

    intel = await _domain_intel(
        domain,
        workspace_id=workspace_id,
        company_id=company.id if company else None,
        company_size_max=company.employee_max if company else None,
        country=company.country if company else None,
    )
    cands = build_candidates(
        first,
        last,
        intel,
        colleagues=colleagues,
        company_size_max=company.employee_max if company else None,
        country=company.country if company else None,
    )
    if not cands:
        reason = (
            "Domain accepts no email" if intel.accepts_mail is False else "No email candidate for this name"
        )
        status = EmailStatus.INVALID if intel.accepts_mail is False else EmailStatus.UNKNOWN
        ms = int((time.monotonic() - t0) * 1000)
        await _log_resolution(
            workspace_id,
            person_id=person_id,
            campaign_id=campaign_id,
            domain=domain,
            path=EmailResolutionPath.fast,
            verdict=None,
            candidates=0,
            smtp_probes=0,
            cache_hits=intel.cache_hits,
            duration_ms=ms,
            status=status,
        )
        return Resolution(
            status,
            None,
            0.0,
            EmailResolutionPath.fast,
            reason=reason,
            cache_hits=intel.cache_hits,
            duration_ms=ms,
        )

    snap = await stats.snapshot()
    smtp_state = await _smtp_state(intel.provider)
    verdicts = evaluate(cands, intel, smtp_state=smtp_state, snap=snap)
    best = pick(verdicts)
    by_addr = {c.address: c for c in cands}
    mode: DeepMode = deep_plan(best, intel, smtp_state) if allow_deep else "none"
    deep = mode == "decide"
    ms = int((time.monotonic() - t0) * 1000)

    if best is not None and best.status != EmailStatus.INVALID:
        await save_finding(
            workspace_id,
            person_id=person_id,
            company_id=company.id if company else None,
            finding=_finding(best, by_addr.get(best.address), intel, None, "fast"),
        )
    await stats.record(
        _stat_events(
            by_addr.get(best.address) if best else None,
            intel,
            EmailResolutionPath.fast,
            correct=None,
            outcome=False,
            latency_ms=ms,
        )
    )
    await _log_resolution(
        workspace_id,
        person_id=person_id,
        campaign_id=campaign_id,
        domain=domain,
        path=EmailResolutionPath.fast,
        verdict=best,
        candidates=len(cands),
        smtp_probes=0,
        cache_hits=intel.cache_hits,
        duration_ms=ms,
    )

    if mode != "none" and best is not None:
        from scout.email.deep import request_deep_verification

        usable = [
            c
            for c, v in zip(cands, verdicts, strict=True)
            if not any(sg.name == "affinity_guard" for sg in v.signals)
        ]
        await request_deep_verification(
            workspace_id=workspace_id,
            person_id=person_id,
            company_id=company.id if company else None,
            # background confirmations never hold a campaign open nor trigger a (second) delivery
            campaign_id=campaign_id if deep else None,
            domain=domain,
            candidates=[c.as_dict() for c in deep_candidates(usable, mode, best)],
            provisional_status=best.status,
            provisional_confidence=best.confidence,
            deliver_context=deliver_context if deep else None,
        )

    if best is None:
        return Resolution(
            EmailStatus.UNKNOWN,
            None,
            0.0,
            EmailResolutionPath.fast,
            candidates=cands,
            reason="No attributable candidate",
            cache_hits=intel.cache_hits,
            duration_ms=ms,
        )
    return Resolution(
        status=best.status,
        address=best.address if best.status != EmailStatus.INVALID else None,
        confidence=best.confidence,
        path=EmailResolutionPath.fast,
        verdict=best,
        candidates=cands,
        deep_requested=deep,
        confirming=mode == "confirm",
        reason=None if best.status in FINAL_FAST else _reason(best),
        cache_hits=intel.cache_hits,
        duration_ms=ms,
    )


def _reason(v: Verdict) -> str:
    return {
        EmailStatus.CATCH_ALL: "Catch-all domain: the address cannot be confirmed",
        EmailStatus.RISKY: "Email not confirmed (moderate evidence)",
        EmailStatus.UNKNOWN: "Not enough evidence for an email",
        EmailStatus.TEMPORARY_UNKNOWN: "Mail server asked to retry later",
        EmailStatus.INVALID: "No valid email address",
    }.get(v.status, "Email not confirmed")
