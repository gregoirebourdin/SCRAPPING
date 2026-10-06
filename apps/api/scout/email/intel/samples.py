"""Observed addresses per domain (``domain_email_samples``): the evidence the learning runs on.

Only on-domain addresses (the domain or a subdomain) are kept, never free-provider ones. Names are
attached only from real records (people of the company, commit authors, RDAP contacts) and the
pattern is *inferred* from them — nothing is invented.

Visibility: public sources (website, GitHub, RDAP, search) are visible to every workspace; private
ones (import, user, smtp_verified) only to the workspace that produced them. All of them feed the
global pattern learning (aggregates only).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scout.db.engine import session_scope
from scout.db.enums import EmailEvidenceSource
from scout.db.models import DomainEmailSample
from scout.email.contracts import ObservedEmail
from scout.email.intel import learning, state
from scout.email.lists import is_disposable_domain, is_free_provider, is_role_local_part
from scout.email.patterns import infer_pattern
from scout.email.syntax import is_valid_syntax, normalize_address, normalize_domain, split_address

PRIVATE_SOURCES: frozenset[EmailEvidenceSource] = frozenset(
    {EmailEvidenceSource.import_, EmailEvidenceSource.user, EmailEvidenceSource.smtp_verified}
)
# Which copy of an address to show when several sources saw it (GitHub is pattern evidence only).
SOURCE_PREFERENCE: tuple[EmailEvidenceSource, ...] = (
    EmailEvidenceSource.user,
    EmailEvidenceSource.website,
    EmailEvidenceSource.import_,
    EmailEvidenceSource.smtp_verified,
    EmailEvidenceSource.search,
    EmailEvidenceSource.rdap,
    EmailEvidenceSource.github,
)


def is_on_domain(address_domain: str, domain: str) -> bool:
    return address_domain == domain or address_domain.endswith("." + domain)


def _name(value: str | None) -> str | None:
    v = (value or "").strip()
    return v or None


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Two observations of the same (address, source) in one batch."""
    out = dict(a)
    out["confidence"] = max(a["confidence"], b["confidence"])
    out["observed_at"] = min(a["observed_at"], b["observed_at"])
    out["last_seen_at"] = max(a["last_seen_at"], b["last_seen_at"])
    if b["first_name"] and b["last_name"] and not (a["first_name"] and a["last_name"]):
        out.update(first_name=b["first_name"], last_name=b["last_name"], pattern=b["pattern"])
    out["is_role"] = a["is_role"] or b["is_role"]
    out["source_url"] = a["source_url"] or b["source_url"]
    out["evidence"] = a["evidence"] or b["evidence"]
    return out


def prepare_samples(
    domain: str, items: Iterable[ObservedEmail], *, workspace_id: uuid.UUID | None, now: datetime
) -> list[dict[str, Any]]:
    """Validated, de-duplicated insert values for on-domain items (pure)."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        addr = normalize_address(item.address)
        if addr is None or not is_valid_syntax(addr):
            continue
        local, adom = split_address(addr)
        if not is_on_domain(adom, domain) or is_free_provider(adom) or is_disposable_domain(adom):
            continue
        first, last = _name(item.first_name), _name(item.last_name)
        if not (first and last):
            first = last = None
        is_role = bool(item.is_role) or is_role_local_part(local)
        pattern = infer_pattern(first, last, local) if first and last and not is_role else None
        seen = item.observed_at or now
        source = EmailEvidenceSource(item.source)
        values = {
            "domain": domain,
            "address": addr,
            "local_part": local,
            "first_name": first,
            "last_name": last,
            "pattern": pattern,
            "is_role": is_role,
            "source": source,
            "source_url": item.source_url,
            "evidence": item.evidence,
            "confidence": min(1.0, max(0.0, float(item.confidence))),
            "times_seen": 1,
            "workspace_id": workspace_id,
            "observed_at": seen,
            "last_seen_at": seen,
        }
        key = (addr, source.value)
        rows[key] = _merge(rows[key], values) if key in rows else values
    return list(rows.values())


async def record_observed_emails(
    domain: str,
    items: Sequence[ObservedEmail],
    *,
    workspace_id: uuid.UUID | None = None,
    relearn: bool = True,
) -> int:
    """Upsert observed addresses for `domain`; returns the number of rows inserted or updated.

    Unique on (domain, address, source): a re-sighting bumps ``times_seen`` and ``last_seen_at``,
    keeps the best confidence and the earliest ``observed_at``; names (and the inferred pattern) are
    replaced only by an observation that carries names. Patterns are relearned unless `relearn=False`.
    """
    d = normalize_domain(domain)
    if d is None or is_free_provider(d) or is_disposable_domain(d) or not items:
        return 0
    values = prepare_samples(d, items, workspace_id=workspace_id, now=state.utcnow())
    if not values:
        return 0
    T = DomainEmailSample
    ins = pg_insert(T).values(values)
    ex = ins.excluded
    named = sa.and_(ex.first_name.is_not(None), ex.last_name.is_not(None))
    stmt = ins.on_conflict_do_update(
        index_elements=[T.domain, T.address, T.source],
        set_={
            "times_seen": T.times_seen + 1,
            "last_seen_at": sa.func.greatest(T.last_seen_at, ex.last_seen_at),
            "observed_at": sa.func.least(T.observed_at, ex.observed_at),
            "confidence": sa.func.greatest(T.confidence, ex.confidence),
            "first_name": sa.case((named, ex.first_name), else_=T.first_name),
            "last_name": sa.case((named, ex.last_name), else_=T.last_name),
            "pattern": sa.case((named, ex.pattern), else_=T.pattern),
            "is_role": sa.or_(T.is_role, ex.is_role),
            "source_url": sa.func.coalesce(ex.source_url, T.source_url),
            "evidence": sa.func.coalesce(ex.evidence, T.evidence),
            "workspace_id": sa.func.coalesce(T.workspace_id, ex.workspace_id),
        },
    )
    async with session_scope() as s:
        await s.execute(stmt)
    if relearn:
        await learning.relearn_domain_patterns(d)
    else:
        state.invalidate(d)
    return len(values)


async def load_observed(domain: str, *, workspace_id: uuid.UUID | None = None) -> list[ObservedEmail]:
    """Observed addresses of a domain (one entry per address and source), named first.

    Private sources (import / user / smtp_verified) are only returned to the workspace that recorded
    them; GitHub entries keep ``source=github`` (pattern evidence, never a contact address).
    """
    d = normalize_domain(domain)
    if d is None:
        return []
    T = DomainEmailSample
    visible: sa.ColumnElement[bool] = T.source.not_in(list(PRIVATE_SOURCES))
    if workspace_id is not None:
        visible = sa.or_(visible, T.workspace_id == workspace_id)
    async with session_scope() as s:
        rows = (
            await s.scalars(
                sa.select(T)
                .where(T.domain == d, visible)
                .order_by(
                    T.is_role.asc(),
                    T.pattern.is_(None).asc(),
                    T.confidence.desc(),
                    T.last_seen_at.desc(),
                    T.address,
                )
            )
        ).all()
    return [learning.observed_from_row(r) for r in rows]


def unique_by_address(observed: Iterable[ObservedEmail]) -> list[ObservedEmail]:
    """One entry per address, preferring the most reusable source (website over GitHub…)."""
    rank = {src: i for i, src in enumerate(SOURCE_PREFERENCE)}
    best: dict[str, ObservedEmail] = {}
    order: list[str] = []
    for o in observed:
        prev = best.get(o.address)
        if prev is None:
            order.append(o.address)
            best[o.address] = o
            continue
        key_new = (rank.get(o.source, 99), -(o.first_name is not None), -o.confidence)
        key_old = (rank.get(prev.source, 99), -(prev.first_name is not None), -prev.confidence)
        if key_new < key_old:
            best[o.address] = o
    return [best[a] for a in order]
