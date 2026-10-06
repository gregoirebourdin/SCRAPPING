"""Deep path: ambiguous candidates verified by SMTP in per-domain batches (docs/EMAIL_ENGINE.md).

Requests (``email_verification_requests``) are created by the engine through
``request_deep_verification`` — one active request per (workspace, person), ≤ 3 ordered candidates.
Every request schedules the ``email.deep_domain`` job of its domain (``dedupe_key="email.deep:<domain>"``),
so ONE job run batches every due request of the domain (across workspaces: SMTP facts are public,
results are persisted per request/workspace) into ONE ``DeepVerifier.probe_domain`` call.

Job run (``process_domain``):

1. claim due requests (``pending``/``retry`` with ``next_attempt_at <= now``, or ``processing`` left
   stale by a dead worker) with ``FOR UPDATE SKIP LOCKED`` → ``processing``;
2. domain intelligence (``scout.email.intel.profile.get_domain_intel`` when available, else the
   ``domain_profiles`` row + MX lookup);
3. health gate (``health.gate(provider, claim_canary=True)``) — when probing is not allowed (BLOCKED,
   disabled, refused identity) or the domain accepts no mail, NO probe is made and every request is
   concluded on a ``not_attempted`` probe (never a negative verdict from missing SMTP);
4. one probe for the union of the candidates (+ random catch-all probes when the profile's catch-all
   is unknown or older than 30 days; the previous random addresses are reused for 24 h so greylisting
   lets them through on the retry); SMTP facts are written back to the profile;
5. per request: ``engine.conclude_after_probe`` → (verdict, retry). Retry → ``retry`` with backoff
   5 min / 30 min / 2 h (never under the 300 s greylisting default), at most 3 retries; otherwise
   ``engine.persist_deep_verdict`` and ``done``. The last attempt is told so (``attempt == max_attempts``)
   and must conclude without INVALID from temporary answers;
6. follow-up job for the earliest pending / retry time of the domain.

Job dedupe: an active job swallows an identical ``dedupe_key``. Immediate work uses
``email.deep:<domain>``; when that job is already running, a follow-up ``email.deep:<domain>:after:<job>``
is queued; scheduled retries use ``email.deep:<domain>@<UTC minute>`` slots, so a 2 h retry never blocks
new requests of the same domain.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.engine import session_scope
from scout.db.enums import JOB_TERMINAL, EmailStatus, JobStatus, MailProvider, SmtpHealthState, SmtpResult
from scout.db.enums import VerificationRequestStatus as RS
from scout.db.models import DomainProfile, EmailVerificationRequest, Job, Person
from scout.email import dns
from scout.email.contracts import DomainIntel, DomainProbeResult, RcptVerdict, SessionOutcome, Verdict
from scout.email.smtp.canary import maybe_run_canary
from scout.email.smtp.classify import is_greylisting, provider_from_mx
from scout.email.smtp.deep_verifiers import DeepVerifier, get_deep_verifier
from scout.email.smtp.health import SmtpHealthMonitor, get_monitor
from scout.email.smtp.session import ProbeReport, probe_summary, restrict_probe
from scout.email.syntax import normalize_address, normalize_domain, split_address
from scout.errors import PermanentError
from scout.jobs.queue import enqueue
from scout.jobs.registry import JobContext, job_handler

log = structlog.get_logger(__name__)

JOB_TYPE = "email.deep_domain"
RETRY_BACKOFF_S: tuple[int, ...] = (300, 1800, 7200)  # 5 min, 30 min, 2 h
MAX_ATTEMPTS = 1 + len(RETRY_BACKOFF_S)  # probe attempts per request (the last one must conclude)
MAX_CANDIDATES = 3
BATCH_MAX_REQUESTS = 50
STALE_PROCESSING_S = 900
CATCH_ALL_MAX_AGE = timedelta(days=30)
RANDOM_PROBES_REUSE_S = 24 * 3600
DEEP_JOB_CONCURRENCY = 4  # per worker: deep jobs never take every worker slot
ACTIVE_STATUSES = (RS.pending, RS.processing, RS.retry)
_ACTIVE_SQL = "status IN ('pending', 'processing', 'retry')"
_RANDOM_PROBES_KEY = "smtp_catch_all_probes"


def dedupe_key(domain: str) -> str:
    return f"email.deep:{domain}"


# --------------------------------------------------------------------------------------------
# Conclusion / persistence (owned by scout.email.engine) — the ONLY integration point
# --------------------------------------------------------------------------------------------


class DeepConcluder(Protocol):
    def conclude(
        self,
        candidates: list[dict[str, Any]],
        intel: DomainIntel | None,
        probe: DomainProbeResult | None,
        *,
        smtp_state: SmtpHealthState,
        attempt: int,
        max_attempts: int,
        first_name: str | None,
        last_name: str | None,
    ) -> tuple[Verdict | None, bool]: ...

    async def persist(
        self,
        request: EmailVerificationRequest,
        verdict: Verdict | None,
        probe: DomainProbeResult | None,
        *,
        smtp_state: SmtpHealthState,
        intel: DomainIntel | None,
    ) -> None: ...


class EngineConcluder:
    """``scout.email.engine.conclude_after_probe`` + ``persist_deep_verdict``."""

    def __init__(self) -> None:
        self._snap: Any = None

    async def prepare(self) -> None:
        """Once per batch: empirical stats snapshot for the confidence engine (best effort)."""
        try:
            from scout.email import stats

            self._snap = await stats.snapshot()
        except Exception as exc:
            log.info("email.deep.stats_snapshot_unavailable", error=str(exc))
            self._snap = None

    def conclude(
        self,
        candidates: list[dict[str, Any]],
        intel: DomainIntel | None,
        probe: DomainProbeResult | None,
        *,
        smtp_state: SmtpHealthState,
        attempt: int,
        max_attempts: int,
        first_name: str | None,
        last_name: str | None,
    ) -> tuple[Verdict | None, bool]:
        from scout.email.engine import conclude_after_probe

        return conclude_after_probe(
            candidates,
            intel,
            probe,
            smtp_state=smtp_state,
            attempt=attempt,
            max_attempts=max_attempts,
            first_name=first_name,
            last_name=last_name,
            snap=self._snap,
        )

    async def persist(
        self,
        request: EmailVerificationRequest,
        verdict: Verdict | None,
        probe: DomainProbeResult | None,
        *,
        smtp_state: SmtpHealthState,
        intel: DomainIntel | None,
    ) -> None:
        from scout.email.engine import persist_deep_verdict

        await persist_deep_verdict(request, verdict, probe, smtp_state=smtp_state, intel=intel)


_concluder: DeepConcluder | None = None


def get_concluder() -> DeepConcluder:
    return _concluder or EngineConcluder()


def set_concluder(concluder: DeepConcluder | None) -> None:
    """Tests: inject a stub conclusion/persistence (None → the engine's)."""
    global _concluder
    _concluder = concluder


# --------------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------------


def _clean_candidates(candidates: list[dict[str, Any]], domain: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        addr = normalize_address(str(c.get("address") or ""))
        if addr is None or addr in seen or split_address(addr)[1] != domain:
            continue
        seen.add(addr)
        out.append({**c, "address": addr})
        if len(out) >= MAX_CANDIDATES:
            break
    return out


async def request_deep_verification(
    *,
    workspace_id: uuid.UUID,
    person_id: uuid.UUID,
    company_id: uuid.UUID | None,
    campaign_id: uuid.UUID | None,
    domain: str,
    candidates: list[dict[str, Any]],
    provisional_status: EmailStatus,
    provisional_confidence: float,
    deliver_context: dict[str, Any] | None = None,
    session: AsyncSession | None = None,
) -> uuid.UUID:
    """Queue (or refresh) the person's deep verification and schedule the domain's batch job.

    Upsert on the active-person index: an active request gets the new candidates / provisional
    status / delivery context. When the candidates changed, the request restarts (``pending``, due now,
    attempts reset; a request being processed is re-queued by the job when it finishes); identical
    candidates keep their retry schedule (never defeat a greylisting backoff).
    """
    d = normalize_domain(domain)
    if d is None:
        raise ValueError(f"invalid domain: {domain!r}")
    cands = _clean_candidates(candidates, d)
    if not cands:
        raise ValueError(f"no candidate address on {d}")
    E = EmailVerificationRequest
    ins = pg_insert(E).values(
        workspace_id=workspace_id,
        person_id=person_id,
        company_id=company_id,
        campaign_id=campaign_id,
        domain=d,
        candidates=cands,
        status=RS.pending,
        attempts=0,
        provisional_status=provisional_status,
        provisional_confidence=provisional_confidence,
        deliver_context=deliver_context,
        result={},
    )
    x = ins.excluded
    same = sa.and_(E.candidates == x.candidates, E.domain == x.domain)
    processing = E.status == RS.processing
    stmt = ins.on_conflict_do_update(
        index_elements=[E.workspace_id, E.person_id],
        index_where=sa.text(_ACTIVE_SQL),
        set_={
            "candidates": x.candidates,
            "domain": x.domain,
            "company_id": x.company_id,
            "campaign_id": x.campaign_id,
            "provisional_status": x.provisional_status,
            "provisional_confidence": x.provisional_confidence,
            "deliver_context": x.deliver_context,
            "status": sa.case((same, E.status), (processing, E.status), else_=sa.literal(RS.pending.value)),
            "attempts": sa.case((same, E.attempts), (processing, E.attempts), else_=0),
            "next_attempt_at": sa.case(
                (same, E.next_attempt_at), (processing, E.next_attempt_at), else_=sa.func.now()
            ),
            "error": sa.case((same, E.error), else_=sa.null()),
            "updated_at": sa.func.now(),
        },
    ).returning(E.id, E.status, E.next_attempt_at)

    async def _do(s: AsyncSession) -> uuid.UUID:
        row = (await s.execute(stmt)).one()
        now = datetime.now(UTC)
        due = row.next_attempt_at if row.status != RS.processing.value else now
        await ensure_domain_job(s, workspace_id=workspace_id, domain=d, run_at=due if due > now else None)
        return row.id

    if session is not None:
        req_id = await _do(session)
    else:
        async with session_scope() as s:
            req_id = await _do(s)
    log.debug("email.deep.requested", domain=d, person_id=str(person_id), candidates=len(cands))
    return req_id


async def cancel_deep_verification(workspace_id: uuid.UUID, person_id: uuid.UUID) -> bool:
    """Cancel the person's active request (e.g. email confirmed by the user meanwhile)."""
    E = EmailVerificationRequest
    async with session_scope() as s:
        res = await s.execute(
            sa.update(E)
            .where(
                E.workspace_id == workspace_id, E.person_id == person_id, E.status.in_([RS.pending, RS.retry])
            )
            .values(status=RS.cancelled, updated_at=sa.func.now())
            .returning(E.id)
        )
        return res.first() is not None


# --------------------------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------------------------


def _slot(at: datetime) -> datetime:
    """Round up to the next UTC minute (retries due in the same minute share one job)."""
    at = at.astimezone(UTC)
    floor = at.replace(second=0, microsecond=0)
    return floor if floor == at else floor + timedelta(minutes=1)


async def ensure_domain_job(
    s: AsyncSession, *, workspace_id: uuid.UUID, domain: str, run_at: datetime | None = None
) -> uuid.UUID | None:
    """Make sure an ``email.deep_domain`` job will run for ``domain`` no later than ``run_at`` (None: now)."""
    now = datetime.now(UTC)
    payload = {"domain": domain}
    if run_at is not None and run_at > now + timedelta(seconds=5):
        slot = _slot(run_at)
        return await enqueue(
            s,
            workspace_id=workspace_id,
            type=JOB_TYPE,
            payload=payload,
            dedupe_key=f"{dedupe_key(domain)}@{slot:%Y%m%dT%H%MZ}",
            run_after=slot,
        )
    key = dedupe_key(domain)
    jid = await enqueue(s, workspace_id=workspace_id, type=JOB_TYPE, payload=payload, dedupe_key=key)
    if jid is not None:
        return jid
    active = (
        await s.execute(
            sa.select(Job.id, Job.status).where(
                Job.workspace_id == workspace_id,
                Job.dedupe_key == key,
                Job.status.notin_([st.value for st in JOB_TERMINAL]),
            )
        )
    ).first()
    if active is None:  # finished in the meantime
        return await enqueue(s, workspace_id=workspace_id, type=JOB_TYPE, payload=payload, dedupe_key=key)
    if active.status in (JobStatus.claimed.value, JobStatus.running.value):
        # The running job may already have claimed its batch: queue exactly one follow-up.
        return await enqueue(
            s,
            workspace_id=workspace_id,
            type=JOB_TYPE,
            payload=payload,
            dedupe_key=f"{key}:after:{active.id}",
        )
    return None  # a pending job will pick the request up


async def claim_due_requests(
    domain: str, *, limit: int = BATCH_MAX_REQUESTS
) -> list[EmailVerificationRequest]:
    """Due requests of a domain → ``processing`` (``FOR UPDATE SKIP LOCKED``: concurrent jobs never overlap)."""
    E = EmailVerificationRequest
    async with session_scope() as s:
        ids = (
            await s.scalars(
                sa.select(E.id)
                .where(
                    E.domain == domain,
                    sa.or_(
                        sa.and_(E.status.in_([RS.pending, RS.retry]), E.next_attempt_at <= sa.func.now()),
                        sa.and_(
                            E.status == RS.processing,
                            E.updated_at < sa.func.now() - timedelta(seconds=STALE_PROCESSING_S),
                        ),
                    ),
                )
                .order_by(E.next_attempt_at, E.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not ids:
            return []
        await s.execute(
            sa.update(E).where(E.id.in_(ids)).values(status=RS.processing, updated_at=sa.func.now())
        )
        rows = (
            await s.scalars(
                sa.select(E)
                .where(E.id.in_(ids))
                .order_by(E.created_at)
                .execution_options(populate_existing=True)
            )
        ).all()
    return list(rows)


async def schedule_followups(domain: str) -> uuid.UUID | None:
    """Queue the next run of the domain at its earliest pending / retry / stale-processing time."""
    E = EmailVerificationRequest
    async with session_scope() as s:
        due_at = sa.case(
            (E.status == RS.processing, E.updated_at + timedelta(seconds=STALE_PROCESSING_S)),
            else_=E.next_attempt_at,
        ).label("due_at")
        row = (
            await s.execute(
                sa.select(E.workspace_id, due_at)
                .where(E.domain == domain, E.status.in_(ACTIVE_STATUSES))
                .order_by(due_at)
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        now = datetime.now(UTC)
        return await ensure_domain_job(
            s, workspace_id=row.workspace_id, domain=domain, run_at=row.due_at if row.due_at > now else None
        )


@job_handler(JOB_TYPE, timeout_s=600.0, max_concurrency=DEEP_JOB_CONCURRENCY)
async def deep_domain_job(ctx: JobContext) -> dict[str, Any]:
    domain = normalize_domain(str(ctx.payload.get("domain") or ""))
    if domain is None:
        raise PermanentError("payload.domain must be a domain name")
    return await process_domain(domain)


# --------------------------------------------------------------------------------------------
# Domain intelligence + SMTP facts (intel layer when available, profile row otherwise)
# --------------------------------------------------------------------------------------------


def _intel_hook(name: str) -> Callable[..., Any] | None:
    try:
        mod = importlib.import_module("scout.email.intel.profile")
    except ImportError:
        return None
    fn = getattr(mod, name, None)
    return fn if callable(fn) else None


@dataclasses.dataclass
class _ProfileMeta:
    catch_all_checked_at: datetime | None = None
    random_probes: list[str] = dataclasses.field(default_factory=list)


async def load_domain_intel(
    domain: str, *, mx_lookup: Callable[[str], Awaitable[dns.MxInfo]] | None = None
) -> tuple[DomainIntel, _ProfileMeta]:
    """``DomainIntel`` for the probe + profile metadata (catch-all freshness, reusable random probes).

    ``mx_lookup`` (default ``dns.mx_lookup``) is only used when neither the intel layer nor the profile
    row knows the MX hosts; a deep verifier may provide its own (``resolve_mx``, e.g. the simulated world).
    """
    async with session_scope() as s:
        row = await s.get(DomainProfile, domain)
    meta = _ProfileMeta()
    if row is not None:
        meta.catch_all_checked_at = row.catch_all_checked_at
        saved = (row.stats or {}).get(_RANDOM_PROBES_KEY) or {}
        at = saved.get("at")
        if isinstance(at, int | float) and datetime.now(UTC).timestamp() - at < RANDOM_PROBES_REUSE_S:
            meta.random_probes = [str(a) for a in saved.get("addresses") or []]
    intel: DomainIntel | None = None
    hook = _intel_hook("get_domain_intel")
    if hook is not None:
        try:
            params = inspect.signature(hook).parameters
            # The deep path needs MX / provider / catch-all only: skip slow public-source components.
            light = {k: False for k in ("include_github", "include_rdap") if k in params}
            intel = await hook(domain, **light)
        except Exception as exc:
            log.warning("email.deep.intel_failed", domain=domain, error=str(exc))
    if intel is None:
        intel = DomainIntel(domain=domain)
        if row is not None:
            intel.provider = MailProvider(row.provider)
            intel.mx_hosts = [str(h) for h in row.mx_hosts or []]
            intel.has_mx, intel.accepts_mail = row.has_mx, row.accepts_mail
            intel.catch_all, intel.catch_all_confidence = row.catch_all, row.catch_all_confidence
            intel.smtp_reachable, intel.greylisting_seen = row.smtp_reachable, bool(row.greylisting_seen)
        if not intel.mx_hosts and intel.accepts_mail is not False:
            info = await (mx_lookup or dns.mx_lookup)(domain)
            if not info.transient:
                intel.mx_hosts = list(info.mx_hosts) or ([domain] if info.has_a and not info.null_mx else [])
                intel.has_mx, intel.accepts_mail = info.has_mx, info.accepts_mail
                if intel.provider == MailProvider.unknown:
                    intel.provider = provider_from_mx(info.mx_hosts, domain=domain)
    return intel, meta


def _greylisted(probe: DomainProbeResult) -> bool:
    if isinstance(probe, ProbeReport):
        return probe.greylisted
    return any(
        v.result == SmtpResult.temporary and is_greylisting(v.message) for v in probe.verdicts.values()
    )


async def write_smtp_facts(domain: str, probe: DomainProbeResult, *, smtp_state: SmtpHealthState) -> None:
    """Catch-all / reachability / greylisting facts back into the domain profile (+ legacy DNS cache)."""
    if probe.session == SessionOutcome.not_attempted:
        return
    hook = _intel_hook("update_smtp_facts")
    handled = False
    if hook is not None:
        try:
            params = inspect.signature(hook).parameters
            kwargs = {"smtp_state": smtp_state} if "smtp_state" in params else {}
            await hook(domain, probe, **kwargs)
            handled = True
        except Exception as exc:
            log.warning("email.deep.update_smtp_facts_failed", domain=domain, error=str(exc))
    now = datetime.now(UTC)
    randoms = list(probe.random_verdicts) if isinstance(probe, ProbeReport) else []
    keep_randoms = probe.catch_all is None and any(
        v.result in (SmtpResult.temporary, SmtpResult.timeout)
        for v in (probe.random_verdicts.values() if isinstance(probe, ProbeReport) else [])
    )
    try:
        async with session_scope() as s:
            await s.execute(
                pg_insert(DomainProfile)
                .values(domain=domain)
                .on_conflict_do_nothing(index_elements=["domain"])
            )
            row = (
                await s.execute(
                    sa.select(DomainProfile).where(DomainProfile.domain == domain).with_for_update()
                )
            ).scalar_one()
            stats = dict(row.stats or {})
            if keep_randoms and randoms:
                stats[_RANDOM_PROBES_KEY] = {"addresses": randoms, "at": now.timestamp()}
            else:
                stats.pop(_RANDOM_PROBES_KEY, None)
            if not handled:
                row.smtp_checked_at = now
                row.smtp_last_result = probe.session.value
                if probe.session in (SessionOutcome.ok, SessionOutcome.temporary):
                    row.smtp_reachable = True
                elif probe.session == SessionOutcome.infra_failure and smtp_state == SmtpHealthState.HEALTHY:
                    row.smtp_reachable = False  # others answer us: this domain's MX is the problem
                if _greylisted(probe):
                    row.greylisting_seen = True
                if probe.catch_all is not None:
                    if row.catch_all is not None and row.catch_all != probe.catch_all:
                        try:
                            from scout.email.intel.state import CATCH_ALL_FLIPPED, add_flag

                            stats = add_flag(
                                stats, CATCH_ALL_FLIPPED, {"from": row.catch_all, "to": probe.catch_all}, now
                            )
                        except ImportError:  # pragma: no cover
                            pass
                    row.catch_all = probe.catch_all
                    row.catch_all_confidence = probe.catch_all_confidence
                    row.catch_all_method = f"smtp_random_probes:{probe.verifier}"
                    row.catch_all_checked_at = now
            row.stats = stats
    except Exception as exc:  # facts are an optimisation: never lose the batch for them
        log.warning("email.deep.profile_write_failed", domain=domain, error=str(exc))
    if probe.catch_all is not None:
        await dns.store_catch_all(domain, probe.catch_all)
    try:
        from scout.email.intel.state import invalidate

        invalidate(domain)
    except ImportError:  # pragma: no cover
        pass


# --------------------------------------------------------------------------------------------
# Batch processing
# --------------------------------------------------------------------------------------------


def _not_attempted(domain: str, addresses: list[str], reason: str, verifier: str) -> ProbeReport:
    return ProbeReport(
        domain=domain,
        session=SessionOutcome.not_attempted,
        verdicts={a: RcptVerdict(a, SmtpResult.not_attempted, None, reason) for a in addresses},
        error=reason,
        verifier=verifier,
    )


def _addresses(req: EmailVerificationRequest) -> list[str]:
    return [str(c["address"]) for c in (req.candidates or []) if isinstance(c, dict) and c.get("address")]


async def _load_names(person_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[str | None, str | None]]:
    if not person_ids:
        return {}
    async with session_scope() as s:
        rows = (
            await s.execute(
                sa.select(Person.id, Person.first_name, Person.last_name, Person.full_name).where(
                    Person.id.in_(person_ids)
                )
            )
        ).all()
    out: dict[uuid.UUID, tuple[str | None, str | None]] = {}
    for r in rows:
        first, last = r.first_name, r.last_name
        tokens = (r.full_name or "").split()
        if not (first and last) and len(tokens) >= 2:
            first, last = first or tokens[0], last or " ".join(tokens[1:])
        out[r.id] = (first, last)
    return out


def _verdict_json(v: Verdict | None) -> dict[str, Any] | None:
    if v is None:
        return None
    return {
        "address": v.address,
        "status": v.status.value,
        "confidence": v.confidence,
        "resolver": v.resolver,
        "affinity": v.affinity,
        "signals": v.explanation,
    }


async def _finalize(req: EmailVerificationRequest, **values: Any) -> bool:
    """Write the request's outcome unless its candidates were replaced meanwhile (→ re-queued, False)."""
    E = EmailVerificationRequest
    async with session_scope() as s:
        res = await s.execute(
            sa.update(E)
            .where(E.id == req.id, E.status == RS.processing, E.candidates == req.candidates)
            .values(updated_at=sa.func.now(), **values)
            .returning(E.id)
        )
        if res.first() is not None:
            return True
        await s.execute(
            sa.update(E)
            .where(E.id == req.id, E.status == RS.processing)
            .values(status=RS.pending, attempts=0, next_attempt_at=sa.func.now(), updated_at=sa.func.now())
        )
    log.info("email.deep.request_resubmitted", request_id=str(req.id))
    return False


def _expansion(
    req: EmailVerificationRequest,
    intel: DomainIntel,
    view: DomainProbeResult,
    smtp_state: SmtpHealthState,
    first: str | None,
    last: str | None,
) -> list[dict[str, Any]]:
    """Next-round candidates when the engine says the first guesses deserve an expansion (else [])."""
    from scout.email.engine import Candidate, expand_candidates, should_expand

    cands = [Candidate.from_dict(c) for c in (req.candidates or [])]
    if not should_expand(cands, intel, view, smtp_state):
        return []
    return [c.as_dict() for c in expand_candidates(first, last, intel, [c.address for c in cands])]


async def _conclude_one(
    req: EmailVerificationRequest,
    *,
    intel: DomainIntel,
    probe: DomainProbeResult,
    smtp_state: SmtpHealthState,
    concluder: DeepConcluder,
    names: dict[uuid.UUID, tuple[str | None, str | None]],
) -> str:
    """Conclusion + persistence for one request → "done" | "retry" | "expanded" | "requeued" | "error"."""
    attempt = int(req.attempts or 0) + 1
    view = restrict_probe(probe, _addresses(req))
    first, last = names.get(req.person_id, (None, None))
    now = datetime.now(UTC)
    backoff = timedelta(seconds=RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S)) - 1])
    try:
        verdict, retry = concluder.conclude(
            list(req.candidates or []),
            intel,
            view,
            smtp_state=smtp_state,
            attempt=attempt,
            max_attempts=MAX_ATTEMPTS,
            first_name=first,
            last_name=last,
        )
    except Exception as exc:
        log.exception("email.deep.conclude_failed", request_id=str(req.id))
        final = attempt >= MAX_ATTEMPTS
        await _finalize(
            req,
            status=RS.done if final else RS.retry,
            attempts=attempt,
            next_attempt_at=now + backoff,
            error=f"conclusion failed: {exc}"[:1000],
        )
        return "error"
    if not retry and (verdict is None or verdict.status == EmailStatus.INVALID):
        expanded = _expansion(req, intel, view, smtp_state, first, last)
        if expanded:
            # every first guess was rejected on a healthy, non catch-all server: one more batched round with the
            # next patterns (no attempt consumed, no verdict persisted yet)
            ok = await _finalize(
                req,
                status=RS.pending,
                candidates=expanded,
                next_attempt_at=now,
                result={
                    "expanded_at": now.isoformat(),
                    "rejected": _addresses(req),
                    "probe": probe_summary(view),
                },
                error=None,
            )
            log.info(
                "email.deep.expanded", request_id=str(req.id), domain=req.domain, candidates=len(expanded)
            )
            return "expanded" if ok else "requeued"
    will_retry = bool(retry) and attempt < MAX_ATTEMPTS
    result = {
        "attempt": attempt,
        "max_attempts": MAX_ATTEMPTS,
        "final": not will_retry,
        "retry_at": (now + backoff).isoformat() if will_retry else None,
        "smtp_state": smtp_state.value,
        "verdict": _verdict_json(verdict),
        "probe": probe_summary(view),
        "concluded_at": now.isoformat(),
    }
    if will_retry:
        ok = await _finalize(
            req, status=RS.retry, attempts=attempt, next_attempt_at=now + backoff, result=result, error=None
        )
        return "retry" if ok else "requeued"
    if retry:
        log.warning("email.deep.retry_refused_last_attempt", request_id=str(req.id), attempt=attempt)
    # Persist with the CURRENT row: the engine may have refreshed the delivery context meanwhile.
    async with session_scope() as s:
        fresh = await s.get(EmailVerificationRequest, req.id, populate_existing=True)
    if fresh is None or fresh.status != RS.processing:
        return "requeued"  # deleted (person removed) or reclaimed elsewhere: nothing to persist here
    if fresh.candidates != req.candidates:
        await _finalize(req, status=RS.done, attempts=attempt)  # candidates replaced → re-queued as pending
        return "requeued"
    try:
        await concluder.persist(fresh, verdict, view, smtp_state=smtp_state, intel=intel)
    except Exception as exc:
        log.exception("email.deep.persist_failed", request_id=str(req.id))
        final = attempt >= MAX_ATTEMPTS
        await _finalize(
            req,
            status=RS.done if final else RS.retry,
            attempts=attempt,
            next_attempt_at=now + backoff,
            result=result,
            error=f"persist failed: {exc}"[:1000],
        )
        return "error"
    ok = await _finalize(req, status=RS.done, attempts=attempt, result=result, error=None)
    return "done" if ok else "requeued"


async def _release(requests: list[EmailVerificationRequest]) -> None:
    """Crash safety: hand unfinished requests back (due in 5 min, no attempt consumed)."""
    E = EmailVerificationRequest
    try:
        async with session_scope() as s:
            await s.execute(
                sa.update(E)
                .where(E.id.in_([r.id for r in requests]), E.status == RS.processing)
                .values(
                    status=RS.retry,
                    next_attempt_at=sa.func.now() + timedelta(seconds=RETRY_BACKOFF_S[0]),
                    updated_at=sa.func.now(),
                )
            )
    except Exception as exc:  # the stale-processing reclaim covers this
        log.warning("email.deep.release_failed", error=str(exc))


async def process_domain(
    domain: str,
    *,
    verifier: DeepVerifier | None = None,
    concluder: DeepConcluder | None = None,
    monitor: SmtpHealthMonitor | None = None,
    limit: int = BATCH_MAX_REQUESTS,
    canary: bool | None = None,
    schedule: bool = True,
) -> dict[str, Any]:
    """One batch for one domain (see module doc). ``canary``: None → settings flag, True → force, False → never."""
    d = normalize_domain(domain)
    if d is None:
        raise ValueError(f"invalid domain: {domain!r}")
    verifier = verifier or get_deep_verifier()
    concluder = concluder or get_concluder()
    monitor = monitor or get_monitor()
    summary: dict[str, Any] = {"domain": d, "claimed": 0, "done": 0, "retry": 0, "requeued": 0, "error": 0}
    requests = await claim_due_requests(d, limit=limit)
    summary["claimed"] = len(requests)
    if requests:
        try:
            summary.update(
                await _process_batch(
                    d, requests, verifier=verifier, concluder=concluder, monitor=monitor, canary=canary
                )
            )
        except BaseException:
            await _release(requests)
            raise
    if schedule:
        job = await schedule_followups(d)
        summary["followup_job"] = str(job) if job else None
    log.info("email.deep.batch", **{k: v for k, v in summary.items() if k != "domain"}, domain=d)
    return summary


async def _process_batch(
    d: str,
    requests: list[EmailVerificationRequest],
    *,
    verifier: DeepVerifier,
    concluder: DeepConcluder,
    monitor: SmtpHealthMonitor,
    canary: bool | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"done": 0, "retry": 0, "requeued": 0, "error": 0, "probed": False}
    intel, meta = await load_domain_intel(d, mx_lookup=getattr(verifier, "resolve_mx", None))
    addresses = list(dict.fromkeys(a for r in requests for a in _addresses(r)))
    provider = intel.provider
    gate = await monitor.gate(provider, claim_canary=True, enabled=verifier.enabled)
    if canary is not False and gate.may_probe:
        refreshed = await maybe_run_canary(
            verifier, gate, monitor=monitor, provider=provider, exclude_domain=d, force=bool(canary)
        )
        if refreshed is not None:
            gate = refreshed
    smtp_state = gate.state

    probe: DomainProbeResult
    if intel.accepts_mail is False:
        probe = _not_attempted(d, addresses, "the domain accepts no mail (no MX / null MX)", verifier.name)
    elif not gate.may_probe:
        reason = (
            "SMTP probing is disabled or refused for this identity"
            if not verifier.enabled
            else f"SMTP health {gate.state.value}: {gate.reason or 'probing paused'}"
        )
        probe = _not_attempted(d, addresses, reason, verifier.name)
    else:
        stale = (
            meta.catch_all_checked_at is None
            or meta.catch_all_checked_at < datetime.now(UTC) - CATCH_ALL_MAX_AGE
        )
        check_catch_all = intel.catch_all is None or stale
        try:
            probe = await verifier.probe_domain(
                d,
                list(intel.mx_hosts),
                addresses,
                check_catch_all=check_catch_all,
                provider=provider if provider not in (MailProvider.unknown, MailProvider.none) else None,
                catch_all_addresses=meta.random_probes or None,
            )
        except Exception as exc:
            log.exception("email.deep.probe_failed", domain=d)
            probe = _not_attempted(d, addresses, f"verifier error: {exc}"[:300], verifier.name)
        out["probed"] = probe.session != SessionOutcome.not_attempted
        if out["probed"]:
            smtp_state = await monitor.current_state(provider, enabled=verifier.enabled)
            await write_smtp_facts(d, probe, smtp_state=smtp_state)
            if probe.catch_all is not None:
                intel = dataclasses.replace(
                    intel, catch_all=probe.catch_all, catch_all_confidence=probe.catch_all_confidence
                )
    out["session"] = probe.session.value
    out["smtp_state"] = smtp_state.value
    out["catch_all"] = probe.catch_all
    out["addresses"] = len(addresses)

    prepare = getattr(concluder, "prepare", None)
    if prepare is not None:
        await prepare()
    names = await _load_names(list({r.person_id for r in requests}))
    for req in requests:
        outcome = await _conclude_one(
            req, intel=intel, probe=probe, smtp_state=smtp_state, concluder=concluder, names=names
        )
        out[outcome] = out.get(outcome, 0) + 1
    return out


def retry_delay_s(attempt: int) -> int:
    """Backoff after the given (1-based) attempt."""
    return RETRY_BACKOFF_S[max(0, min(attempt, len(RETRY_BACKOFF_S)) - 1)]


__all__ = [
    "JOB_TYPE",
    "MAX_ATTEMPTS",
    "RETRY_BACKOFF_S",
    "DeepConcluder",
    "EngineConcluder",
    "cancel_deep_verification",
    "claim_due_requests",
    "deep_domain_job",
    "ensure_domain_job",
    "get_concluder",
    "process_domain",
    "request_deep_verification",
    "retry_delay_s",
    "schedule_followups",
    "set_concluder",
]
