"""Explainable email confidence engine (docs/EMAIL_ENGINE.md §Confidence).

Each candidate starts from a base probability — the empirical precision of the resolver that produced
it (published, GitHub, learned domain pattern, prior permutation…) — and measurable signals move it in
log-odds space: name affinity, MX, SMTP (only when our infrastructure is healthy), catch-all, free
provider. Hard rules decide INVALID; the status ladder then maps evidence to

    SAFE > LIKELY_SAFE > RISKY > CATCH_ALL > TEMPORARY_UNKNOWN > UNKNOWN > INVALID

Every verdict carries its signals, so the UI can explain it. Never conclude negatively from an
unhealthy SMTP path; never call a guess SAFE because a catch-all domain accepted it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scout.db.enums import EmailStatus, SmtpHealthState, SmtpResult
from scout.email.affinity import ATTRIBUTE_MIN, STRONG, Affinity
from scout.email.contracts import Signal, Verdict
from scout.email.stats import StatRow, describe, precision

# Default priors per resolver (P(address is right) before any verification). Learned over time.
RESOLVER_PRIORS: dict[str, float] = {
    "user": 0.99,
    "cache": 0.95,
    "published_website": 0.93,
    "import": 0.88,
    "smtp_verified": 0.95,
    "github": 0.82,
    "search": 0.75,
    "rdap": 0.6,
}

LIKELY_SAFE_MIN = 0.80
RISKY_MIN = 0.50
FAST_ACCEPT_MIN = LIKELY_SAFE_MIN

_W_AFFINITY = 2.5
_W_MX = 0.25
_W_SMTP_ACCEPT_PROVEN = 3.5
_W_SMTP_ACCEPT_UNPROVEN = 0.8
_W_CATCH_ALL_GUESS = -0.8
_W_FREE_PROVIDER = -1.2
_CAP = 0.99

STATUS_RANK: dict[EmailStatus, int] = {
    EmailStatus.INVALID: 0,
    EmailStatus.UNKNOWN: 1,
    EmailStatus.TEMPORARY_UNKNOWN: 2,
    EmailStatus.CATCH_ALL: 3,
    EmailStatus.RISKY: 4,
    EmailStatus.LIKELY_SAFE: 5,
    EmailStatus.SAFE: 6,
}


def logit(p: float) -> float:
    p = min(1 - 1e-6, max(1e-6, p))
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


@dataclass
class Evidence:
    """Everything measurable about one candidate address."""

    address: str
    resolver: str  # published_website | github | rdap | import | user | cache | domain_pattern | inferred_shape | permutation
    base_probability: float  # resolver/pattern prior, before signals
    base_detail: str
    affinity: Affinity
    published_on_domain: bool = False  # published by the company itself for this person
    source_url: str | None = None
    has_mx: bool | None = None
    accepts_mail: bool | None = None
    catch_all: bool | None = None
    catch_all_confidence: float | None = None
    smtp: SmtpResult | None = None  # None: not probed
    smtp_state: SmtpHealthState = SmtpHealthState.UNKNOWN
    smtp_detail: str | None = None
    free_provider: bool = False
    disposable: bool = False
    syntax_valid: bool = True
    retry_pending: bool = False  # a temporary SMTP answer will be re-checked


def _base(ev: Evidence, snap: dict[tuple[str, str], StatRow] | None) -> tuple[float, Signal]:
    prior = ev.base_probability
    if ev.resolver in RESOLVER_PRIORS:
        prior = precision("resolver", ev.resolver, RESOLVER_PRIORS[ev.resolver], snap)
    detail = ev.base_detail
    hist = describe("resolver", ev.resolver, snap)
    if hist:
        detail = f"{detail} ({hist})"
    return prior, Signal(
        "base", 0.0, f"{detail} → {round(prior * 100)}% prior", source=ev.resolver, source_url=ev.source_url
    )


def assess(ev: Evidence, snap: dict[tuple[str, str], StatRow] | None = None) -> Verdict:
    """Status + confidence + explanation for one candidate."""
    signals: list[Signal] = []

    def verdict(status: EmailStatus, conf: float) -> Verdict:
        return Verdict(
            ev.address, status, round(min(_CAP, max(0.0, conf)), 4), signals, ev.resolver, ev.affinity.score
        )

    # ---- hard failures --------------------------------------------------------------------
    if not ev.syntax_valid:
        signals.append(Signal("syntax", -10, "Invalid address syntax"))
        return verdict(EmailStatus.INVALID, 0.0)
    if ev.disposable:
        signals.append(Signal("disposable", -10, "Disposable email domain"))
        return verdict(EmailStatus.INVALID, 0.0)
    if ev.accepts_mail is False:
        signals.append(Signal("no_mx", -10, "Domain accepts no email (no MX / null MX)"))
        return verdict(EmailStatus.INVALID, 0.0)
    if ev.affinity.score < ATTRIBUTE_MIN:
        signals.append(
            Signal("affinity_guard", -10, f"Not attributable to this person: {ev.affinity.reason}")
        )
        return verdict(EmailStatus.UNKNOWN, 0.0)
    if ev.smtp == SmtpResult.rejected and ev.catch_all is not True:
        signals.append(
            Signal("smtp_rejected", -10, ev.smtp_detail or "Mail server: this mailbox does not exist")
        )
        return verdict(EmailStatus.INVALID, 0.02)

    # ---- probability ----------------------------------------------------------------------
    p0, base_signal = _base(ev, snap)
    signals.append(base_signal)
    x = logit(p0)

    w = _W_AFFINITY * (ev.affinity.score - STRONG)
    x += w
    signals.append(
        Signal("name_affinity", w, f"Name affinity {round(ev.affinity.score * 100)}% — {ev.affinity.reason}")
    )

    if ev.has_mx:
        x += _W_MX
        signals.append(Signal("mx_valid", _W_MX, "Domain has valid MX records"))

    if ev.free_provider:
        x += _W_FREE_PROVIDER
        signals.append(Signal("free_provider", _W_FREE_PROVIDER, "Free / personal mailbox provider"))

    smtp_usable = ev.smtp_state != SmtpHealthState.BLOCKED
    proven = False
    if ev.smtp == SmtpResult.accepted and smtp_usable:
        if ev.catch_all is False:
            x += _W_SMTP_ACCEPT_PROVEN
            proven = True
            signals.append(
                Signal(
                    "smtp_accepted",
                    _W_SMTP_ACCEPT_PROVEN,
                    "Mail server accepted this mailbox (domain is not catch-all)",
                )
            )
        elif ev.catch_all is None:
            x += _W_SMTP_ACCEPT_UNPROVEN
            signals.append(
                Signal(
                    "smtp_accepted",
                    _W_SMTP_ACCEPT_UNPROVEN,
                    "Mail server accepted it, catch-all status unknown",
                )
            )
        else:
            signals.append(
                Signal("smtp_accepted", 0.0, "Accepted, but the domain accepts every address (uninformative)")
            )
    elif ev.smtp in (SmtpResult.temporary, SmtpResult.timeout):
        signals.append(
            Signal(
                "smtp_temporary",
                0.0,
                ev.smtp_detail or "Mail server asked to try again later (no conclusion)",
            )
        )
    elif ev.smtp in (SmtpResult.blocked, SmtpResult.unknown, SmtpResult.not_attempted) or not smtp_usable:
        if ev.smtp is not None or not smtp_usable:
            signals.append(
                Signal(
                    "smtp_unavailable", 0.0, "SMTP verification unavailable — no negative conclusion drawn"
                )
            )

    if ev.catch_all is True and not ev.published_on_domain:
        x += _W_CATCH_ALL_GUESS
        signals.append(
            Signal("catch_all", _W_CATCH_ALL_GUESS, "Catch-all domain: a guessed address cannot be confirmed")
        )

    p = sigmoid(x)

    # ---- status ladder --------------------------------------------------------------------
    strong = ev.affinity.score >= STRONG
    if proven and strong:
        return verdict(EmailStatus.SAFE, max(p, 0.95))
    if proven:  # the mailbox exists, but a weak name match (e.g. "john@") could be another John
        return verdict(EmailStatus.LIKELY_SAFE, min(max(p, 0.8), 0.9))
    if ev.published_on_domain and strong and ev.has_mx is not False:
        return verdict(EmailStatus.SAFE, max(p, 0.9 if ev.catch_all else 0.92))
    if ev.retry_pending and ev.smtp in (SmtpResult.temporary, SmtpResult.timeout):
        return verdict(EmailStatus.TEMPORARY_UNKNOWN, p)
    if ev.catch_all is True:
        return verdict(EmailStatus.CATCH_ALL, p)
    if p >= LIKELY_SAFE_MIN and strong and ev.has_mx and not ev.free_provider:
        return verdict(EmailStatus.LIKELY_SAFE, p)
    if p >= RISKY_MIN:
        return verdict(EmailStatus.RISKY, p)
    return verdict(EmailStatus.UNKNOWN, p)


def better(a: Verdict, b: Verdict) -> bool:
    """Whether verdict ``a`` should be preferred over ``b``."""
    return (STATUS_RANK[a.status], a.confidence) > (STATUS_RANK[b.status], b.confidence)
