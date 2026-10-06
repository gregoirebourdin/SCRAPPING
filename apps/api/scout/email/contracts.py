"""Email Intelligence Engine contracts (docs/EMAIL_ENGINE.md).

Shared, dependency-free data types between the three layers:

* ``scout.email.intel``  — Domain Intelligence Profiles (MX, provider, observed emails, learned patterns,
  catch-all, SMTP behaviour), computed once per domain and reused everywhere.
* ``scout.email.smtp``   — our SMTP verification path: per-domain batched probing, catch-all detection,
  health monitoring (HEALTHY / DEGRADED / BLOCKED / UNKNOWN), greylist-aware classification.
* ``scout.email.engine`` — fast path / deep path orchestration, name-affinity guard, explainable
  confidence engine and empirical resolver statistics.

Nothing here performs I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from scout.db.enums import EmailEvidenceSource, EmailStatus, MailProvider, SmtpHealthState, SmtpResult

# --------------------------------------------------------------------------------------------
# Domain intelligence
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservedEmail:
    """A real address seen for a domain, with its provenance."""

    address: str
    local_part: str
    source: EmailEvidenceSource
    first_name: str | None = None
    last_name: str | None = None
    pattern: str | None = None  # inferred when (first, last) are known
    is_role: bool = False
    source_url: str | None = None
    evidence: str | None = None
    confidence: float = 0.9
    observed_at: datetime | None = None


@dataclass(frozen=True)
class PatternStat:
    """Learned convention for a domain, e.g. ``{first}.{last}`` = 0.96 from 8 samples."""

    pattern: str
    share: float  # weighted share among the domain's named samples (0–1)
    confidence: float  # posterior probability that a new employee follows this pattern (0–1)
    samples: int
    successes: int = 0  # SMTP-confirmed guesses
    failures: int = 0  # SMTP-rejected guesses (healthy infrastructure only)
    last_confirmed_at: datetime | None = None


@dataclass
class DomainIntel:
    """Domain Intelligence Profile, as consumed by the engine."""

    domain: str
    provider: MailProvider = MailProvider.unknown
    mx_hosts: list[str] = field(default_factory=list)
    has_mx: bool | None = None
    accepts_mail: bool | None = None  # False: null MX, or no MX and no A record
    catch_all: bool | None = None
    catch_all_confidence: float | None = None
    smtp_reachable: bool | None = None
    greylisting_seen: bool = False
    patterns: list[PatternStat] = field(default_factory=list)  # best first
    observed: list[ObservedEmail] = field(default_factory=list)  # on-domain addresses (named + role)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    cache_hits: dict[str, bool] = field(default_factory=dict)  # component -> served from cache
    built_at: datetime | None = None

    @property
    def dominant(self) -> PatternStat | None:
        return self.patterns[0] if self.patterns else None

    @property
    def named_samples(self) -> int:
        return sum(1 for o in self.observed if o.first_name and o.last_name and not o.is_role)


# --------------------------------------------------------------------------------------------
# SMTP
# --------------------------------------------------------------------------------------------


class SessionOutcome(StrEnum):
    """What a probe session says about OUR infrastructure (not about mailboxes)."""

    ok = "ok"  # reached RCPT stage
    temporary = "temporary"  # 4xx / greylisting at session level
    infra_failure = "infra_failure"  # connect refused/timeout on port 25, DNS failure for MX host
    policy_block = "policy_block"  # 5.7.x / blocklist / reputation: our IP or HELO refused
    not_attempted = "not_attempted"  # SMTP disabled or health gate closed


@dataclass(frozen=True)
class RcptVerdict:
    address: str
    result: SmtpResult  # accepted | rejected | temporary | unknown | timeout | blocked | not_attempted
    code: int | None = None
    message: str = ""


@dataclass
class DomainProbeResult:
    """One SMTP session against one domain: several RCPTs + optional random catch-all probes."""

    domain: str
    session: SessionOutcome
    verdicts: dict[str, RcptVerdict] = field(default_factory=dict)  # by address
    catch_all: bool | None = None
    catch_all_confidence: float | None = None
    mx_host: str | None = None
    probes: int = 0  # RCPT commands sent (targets + random)
    duration_ms: int = 0
    error: str | None = None
    verifier: str = "builtin"


# --------------------------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    """One explainable contribution to an email's confidence (log-odds space)."""

    name: str  # e.g. "published_on_site", "pattern_confirmed", "mx_valid", "smtp_accepted", "catch_all"
    weight: float  # log-odds delta (positive supports the address)
    detail: str  # human sentence shown in the UI
    source: str | None = None
    source_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight": round(self.weight, 3),
            "detail": self.detail,
            "source": self.source,
            "source_url": self.source_url,
        }


@dataclass
class Verdict:
    """Final (or provisional) assessment of one candidate address."""

    address: str
    status: EmailStatus
    confidence: float  # 0–1
    signals: list[Signal] = field(default_factory=list)
    resolver: str | None = None  # "published_website", "domain_pattern", "github", "permutation", …
    affinity: float | None = None

    @property
    def explanation(self) -> list[dict[str, Any]]:
        return [s.as_dict() for s in self.signals]


SMTP_USABLE: frozenset[SmtpHealthState] = frozenset(
    {SmtpHealthState.HEALTHY, SmtpHealthState.DEGRADED, SmtpHealthState.UNKNOWN}
)
