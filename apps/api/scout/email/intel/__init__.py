"""Domain intelligence layer of the Email Intelligence Engine (see ``scout.email.contracts``).

Per DOMAIN, not per contact: MX + provider, observed addresses (website, GitHub commits, RDAP, imports,
SMTP-verified), learned patterns and SMTP facts are computed once and reused by every contact,
campaign and workspace, subject to per-component freshness. Evidence priority: published / observed
> learned pattern > generated candidate. No paid API, no LLM, nothing invented.

Public API::

    detect_provider(mx_hosts, *, domain=None) -> MailProvider
    provider_notes(provider, mx_hosts=()) -> dict
    learn(domain, samples, *, successes, failures, ...) -> list[PatternStat]          # pure
    relearn_domain_patterns(domain, *, company_size_max=None, country=None) -> list[PatternStat]
    record_pattern_outcome(domain, pattern, success) -> None
    load_pattern_stats(domain) -> list[PatternStat]
    record_observed_emails(domain, items, *, workspace_id=None, relearn=True) -> int
    load_observed(domain, *, workspace_id=None) -> list[ObservedEmail]
    get_domain_intel(domain, *, workspace_id, company_id, refresh, include_github, include_rdap,
                     company_size_max, country) -> DomainIntel
    update_smtp_facts(domain, probe=None, *, catch_all, catch_all_confidence, catch_all_method,
                      smtp_reachable, smtp_last_result, greylisting_seen, smtp_state=None) -> None
"""

from __future__ import annotations

from scout.email.intel.learning import (
    learn,
    load_pattern_stats,
    record_pattern_outcome,
    relearn_domain_patterns,
)
from scout.email.intel.profile import (
    flush_stats,
    get_domain_intel,
    profile_invalidated_at,
    update_smtp_facts,
)
from scout.email.intel.providers import detect_provider, provider_notes
from scout.email.intel.samples import load_observed, record_observed_emails

__all__ = [
    "detect_provider",
    "flush_stats",
    "get_domain_intel",
    "learn",
    "load_observed",
    "load_pattern_stats",
    "profile_invalidated_at",
    "provider_notes",
    "record_observed_emails",
    "record_pattern_outcome",
    "relearn_domain_patterns",
    "update_smtp_facts",
]
