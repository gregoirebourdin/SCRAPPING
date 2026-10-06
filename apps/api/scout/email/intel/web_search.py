"""On-domain addresses published elsewhere on the web, found by free web search (SearXNG).

Directories, PDFs, press releases and event pages often print ``prenom.nom@agence.fr``. Every such address is
a real sample of the domain's convention (``source=search``, weight 0.6 in pattern learning) and, when it
belongs to the person being resolved, a published address. Used only when the company's own pages did not
already show a named address, at most once per domain per ``SEARCH_FRESHNESS``. Free search only — no
Gemini, no paid API; a no-op ([], not completed) without a healthy lookup provider.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

import structlog

from scout.db.enums import EmailEvidenceSource
from scout.email.contracts import ObservedEmail
from scout.email.intel import samples
from scout.email.lists import is_role_local_part
from scout.email.syntax import normalize_address, split_address

log = structlog.get_logger(__name__)

SEARCH_FRESHNESS = timedelta(days=30)
SEARCH_ERROR_BACKOFF = timedelta(hours=6)
SEARCH_SAMPLE_CONFIDENCE = 0.7
MAX_ADDRESSES = 20

_EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")


@dataclass
class WebSearchEvidence:
    emails: list[ObservedEmail] = field(default_factory=list)
    completed: bool = False
    queries: int = 0
    results: int = 0
    error: str | None = None


def addresses_in(text: str, domain: str) -> list[str]:
    """On-domain addresses in a title / snippet / URL (lower-cased, de-duplicated, in order)."""
    out: list[str] = []
    for m in _EMAIL_RE.finditer(text or ""):
        addr = normalize_address(m.group(0).strip(".-"))
        if addr is None or addr in out:
            continue
        if samples.is_on_domain(split_address(addr)[1], domain):
            out.append(addr)
    return out


async def fetch_search_evidence(
    domain: str,
    *,
    people: Sequence[tuple[str | None, str | None]] = (),
    chain: object | None = None,
) -> WebSearchEvidence:
    from scout.email.intel.profile import match_person
    from scout.search.chain import SearchChain, lookup_chain

    ch = chain if isinstance(chain, SearchChain) else lookup_chain("email_samples")
    if not ch.available():
        return WebSearchEvidence(error="no search provider")
    ev = WebSearchEvidence()
    try:
        res = await ch.search(f'"@{domain}"', num=10)
    except Exception as exc:  # search is best-effort evidence, never fails a resolution
        log.info("email.intel.search_failed", domain=domain, error=str(exc))
        return WebSearchEvidence(queries=1, error=str(exc)[:200])
    ev.queries = 1
    if res.provider is None:  # every provider failed or is cooling down: try again later
        ev.error = "no provider answered"
        return ev
    ev.results = len(res.results)
    seen: set[str] = set()
    for r in res.results:
        for addr in addresses_in(f"{r.title} {r.snippet} {r.url}", domain):
            if addr in seen or len(ev.emails) >= MAX_ADDRESSES:
                continue
            seen.add(addr)
            local = split_address(addr)[0]
            role = is_role_local_part(local)
            names = None if role else match_person(local, people)
            ev.emails.append(
                ObservedEmail(
                    address=addr,
                    local_part=local,
                    source=EmailEvidenceSource.search,
                    first_name=names[0] if names else None,
                    last_name=names[1] if names else None,
                    is_role=role,
                    source_url=r.url,
                    evidence=f"published on {r.url}"[:300],
                    confidence=SEARCH_SAMPLE_CONFIDENCE,
                )
            )
    ev.completed = True
    return ev
