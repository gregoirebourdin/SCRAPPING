"""Email engine benchmark: labelled scenarios × simulated mail world × strategies (docs/EMAIL_ENGINE.md §Benchmark).

Strategies
    legacy             v1 waterfall (``finder.find_email``): up to 6 candidates per person, one SMTP session per
                       address, stop at the first SAFE; pre-engine classification (4xx/timeouts = unknown, no retry).
    fast_only          domain intelligence + learned patterns + ≤ 3 candidates + confidence engine, no SMTP.
    fast_deep          fast path, then ONE batched SMTP session per domain for ambiguous cases (random catch-all
                       probes, greylist retries with backoff) and background confirmation of thin-evidence
                       LIKELY_SAFE verdicts — the production policy (``engine.deep_plan``).
    fast_deep_blocked  fast_deep with outbound port 25 blocked (e.g. Railway Hobby): the health monitor must stop
                       probing and no verdict may become INVALID because of it.

The engine strategies call the production decision functions (``build_candidates``, ``evaluate``, ``pick``,
``deep_plan``, ``conclude_after_probe``, ``intel.learning.learn``) and the production SMTP batching/classification
code through ``WorldDeepVerifier``; only persistence is replaced by in-memory domain state. Each dataset is run in
``waves`` (a later campaign on the same domains) so learning and caching are measured too.

Times are simulated (world SMTP latency + the ``LatencyModel``); engine CPU time is measured. Costs use the explicit
``CostModel`` assumptions: no paid API is involved, the unit costs only stand for compute and IP reputation.
Synthetic results validate behaviour and compare strategies; they are NOT a precision claim about real domains.
"""

from __future__ import annotations

import heapq
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from scout.db.enums import EmailDiscoveryMethod, EmailEvidenceSource, EmailStatus, SmtpHealthState, SmtpResult
from scout.email.confidence import STATUS_RANK
from scout.email.contracts import DomainIntel, DomainProbeResult, ObservedEmail, SessionOutcome
from scout.email.deep import merge_probes
from scout.email.engine import (
    Candidate,
    build_candidates,
    conclude_after_probe,
    deep_candidates,
    deep_plan,
    evaluate,
    expand_candidates,
    narrow_after_pilot,
    pick,
    pick_pilot,
    should_expand,
)
from scout.email.finder import find_email
from scout.email.intel.learning import learn
from scout.email.intel.providers import detect_provider
from scout.email.lists import is_role_local_part
from scout.email.patterns import infer_pattern, infer_patterns, pattern_confidence
from scout.email.smtp.health import MemoryHealthStore, SmtpHealthMonitor
from scout.email.smtp.world import MailWorld, WorldDeepVerifier
from scout.email.syntax import split_address
from scout.email.types import VerificationResult

from .email_scenarios import BenchDataset, BenchDomain, BenchPerson, generate

STRATEGIES: dict[str, str] = {
    "legacy": "v1 waterfall: ≤ 6 candidates, one SMTP session per address, no retry",
    "fast_only": "domain intelligence + learned pattern + ≤ 3 candidates, no SMTP",
    "fast_deep": "fast path + one batched SMTP session per ambiguous domain (production policy)",
    "fast_deep_blocked": "fast_deep with outbound port 25 blocked",
}
ACCEPTED: frozenset[EmailStatus] = frozenset({EmailStatus.SAFE, EmailStatus.LIKELY_SAFE})
MAX_DEEP_ATTEMPTS = 4  # same as the deep job
BACKOFF_S = (300, 1800, 7200)


@dataclass(frozen=True)
class LatencyModel:
    dns_ms: int = 35  # MX lookup when the domain profile is not cached
    profile_ms: int = 4  # profile assembly (learning, provider) per domain build
    person_ms: int = 1  # candidates + confidence per person (measured CPU reported separately)
    smtp_overhead_ms: int = (
        300  # TCP + banner + EHLO + MAIL FROM per session, on top of the world's RCPT latency
    )
    fast_workers: int = 16
    smtp_workers: int = 4


@dataclass(frozen=True)
class CostModel:
    smtp_session_usd: float = 0.00002
    smtp_rcpt_usd: float = 0.000005
    dns_query_usd: float = 0.0
    note: str = "assumed unit costs (no paid API): compute + IP reputation; override for your infrastructure"


@dataclass
class Outcome:
    domain: str
    kind: str
    person: str
    truth: str | None
    address: str | None  # address offered for the person (None when INVALID/UNKNOWN without address)
    judged: str | None  # address the verdict is about (also for INVALID)
    status: EmailStatus
    confidence: float
    resolver: str | None
    wave: int
    path: str = "fast"
    first_ms: int = 0
    final_ms: int = 0  # includes retry backoff waits
    deep_attempts: int = 0
    profile_cached: bool = False
    cpu_ms: float = 0.0


@dataclass
class DomainMemory:
    """What the engine (or legacy store) remembers about a domain between waves."""

    built: bool = False
    catch_all: bool | None = None
    catch_all_confidence: float | None = None
    successes: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)
    verified: list[ObservedEmail] = field(default_factory=list)
    catch_all_probes: list[str] = field(
        default_factory=list
    )  # reused like domain_profiles.stats (greylisting)
    legacy_catch_all: bool | None = None


@dataclass
class Resources:
    dns_queries: int = 0
    smtp_sessions: int = 0
    smtp_rcpts: int = 0
    expansions: int = 0
    pilots: int = 0
    chains: list[tuple[int, list[int]]] = field(
        default_factory=list
    )  # (fast_ms, [smtp session ms…]) per domain-wave


class _SimClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class _Run:
    ds: BenchDataset
    strategy: str
    world: MailWorld
    verifier: WorldDeepVerifier
    monitor: SmtpHealthMonitor
    clock: _SimClock
    lat: LatencyModel
    memory: dict[str, DomainMemory] = field(default_factory=dict)
    res: Resources = field(default_factory=Resources)
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def smtp(self) -> bool:
        return self.strategy != "fast_only"


# ---------------------------------------------------------------------------------------------------------------
# World + intel
# ---------------------------------------------------------------------------------------------------------------


def build_world(ds: BenchDataset, *, port25_blocked: bool = False) -> MailWorld:
    world = MailWorld(seed=ds.seed, port25_blocked=port25_blocked)
    for d in ds.domains:
        if not d.accepts_mail:
            continue  # NXDOMAIN: no MX, no A
        world.add(
            d.domain,
            mailboxes=set(d.mailboxes),
            catch_all=d.catch_all,
            provider=d.provider,
            behaviour=d.behaviour,  # type: ignore[arg-type]
        )
    return world


def _observed(d: BenchDomain) -> list[ObservedEmail]:
    out: list[ObservedEmail] = []
    for o in d.observed:
        local, _ = split_address(o.address)
        named = bool(o.first and o.last)
        out.append(
            ObservedEmail(
                address=o.address,
                local_part=local,
                source=o.source,
                first_name=o.first,
                last_name=o.last,
                pattern=infer_pattern(o.first, o.last, local) if named else None,
                is_role=is_role_local_part(local),
                source_url=o.source_url,
            )
        )
    return out


def _intel(d: BenchDomain, mx: Any, mem: DomainMemory) -> DomainIntel:
    observed = _observed(d) + mem.verified
    patterns = learn(
        d.domain,
        observed,
        successes=dict(mem.successes),
        failures=dict(mem.failures),
        company_size_max=d.company_size_max,
        country=d.country,
    )
    return DomainIntel(
        domain=d.domain,
        provider=detect_provider(mx.mx_hosts, domain=d.domain),
        mx_hosts=list(mx.mx_hosts),
        has_mx=mx.has_mx,
        accepts_mail=mx.accepts_mail,
        catch_all=mem.catch_all,
        catch_all_confidence=mem.catch_all_confidence,
        patterns=patterns,
        observed=observed,
    )


def _everyone(d: BenchDomain) -> list[tuple[str | None, str | None]]:
    """Names known at the company (named observations + targets): the affinity guard's colleagues."""
    names: list[tuple[str | None, str | None]] = [(o.first, o.last) for o in d.observed if o.first and o.last]
    return names + [(t.first, t.last) for t in d.targets]


def _waves(people: list[BenchPerson], waves: int) -> list[list[BenchPerson]]:
    """Split a domain's targets into consecutive campaigns (first campaign gets the larger half)."""
    if waves <= 1 or len(people) < 2:
        return [people] + [[] for _ in range(max(0, waves - 1))]
    size = -(-len(people) // waves)
    return [people[i * size : (i + 1) * size] for i in range(waves)]


# ---------------------------------------------------------------------------------------------------------------
# Engine strategies
# ---------------------------------------------------------------------------------------------------------------


def _learn_from_probe(
    mem: DomainMemory,
    cands: list[Candidate],
    probe: DomainProbeResult,
    state: SmtpHealthState,
    p: BenchPerson,
) -> None:
    """Same rule as ``engine._learn_from_probe``: only healthy, non catch-all answers teach the domain."""
    if probe.catch_all is not False or state == SmtpHealthState.BLOCKED:
        return
    for c in cands:
        rv = probe.verdicts.get(c.address)
        if rv is None:
            continue
        if rv.result == SmtpResult.accepted:
            if c.pattern:
                mem.successes[c.pattern] += 1
            local, _ = split_address(c.address)
            mem.verified.append(
                ObservedEmail(
                    address=c.address,
                    local_part=local,
                    source=EmailEvidenceSource.smtp_verified,
                    first_name=p.first,
                    last_name=p.last,
                    pattern=c.pattern,
                    confidence=0.95,
                )
            )
        elif rv.result == SmtpResult.rejected and c.pattern:
            mem.failures[c.pattern] += 1


def _apply(out: Outcome, verdict: Any, mode: str) -> None:
    if verdict is None:
        return
    if mode == "confirm" and verdict.status not in (EmailStatus.SAFE, EmailStatus.INVALID):
        if STATUS_RANK[verdict.status] < STATUS_RANK[out.status]:
            return  # inconclusive background confirmation keeps the fast verdict (same as persist_deep_verdict)
    out.status = verdict.status
    out.confidence = verdict.confidence
    out.resolver = verdict.resolver or out.resolver
    out.judged = verdict.address
    out.address = verdict.address if verdict.status != EmailStatus.INVALID else None


async def _deep(
    run: _Run,
    d: BenchDomain,
    intel: DomainIntel,
    pending: list[tuple[BenchPerson, Outcome, list[Candidate], str]],
    sessions: list[int],
    attempt: int = 1,
) -> None:
    mem = run.memory[d.domain]
    gate = await run.monitor.gate(intel.provider, claim_canary=True, enabled=True)
    if not gate.may_probe:
        for p, out, cands, mode in pending:
            v, _ = conclude_after_probe(
                [c.as_dict() for c in cands],
                intel,
                None,
                smtp_state=gate.state,
                attempt=MAX_DEEP_ATTEMPTS,
                max_attempts=MAX_DEEP_ATTEMPTS,
                first_name=p.first,
                last_name=p.last,
            )
            out.path = "deep"
            _apply(out, v, mode)
        return

    async def probe_once(addrs: list[str], check_catch_all: bool) -> DomainProbeResult:
        w0, r0 = run.world.simulated_latency_ms, run.world.rcpt_commands
        p = await run.verifier.probe_domain(
            d.domain,
            intel.mx_hosts,
            addrs,
            check_catch_all=check_catch_all,
            provider=intel.provider,
            catch_all_addresses=(mem.catch_all_probes or None) if check_catch_all else None,
        )
        if check_catch_all and not mem.catch_all_probes:
            mem.catch_all_probes = list(getattr(p, "random_verdicts", {}) or {})
        sessions.append((run.world.simulated_latency_ms - w0) + run.lat.smtp_overhead_ms)
        run.res.smtp_sessions += 1
        run.res.smtp_rcpts += run.world.rcpt_commands - r0
        return p

    # same plan as the deep job (_probe_with_pilot): pilot person first when the convention is unknown
    n_sessions = len(sessions)
    batch = [cands for _, _, cands, _ in pending]
    pilot_idx = pick_pilot(batch, intel)
    if pilot_idx is None:
        addrs = list(dict.fromkeys(c.address for cands in batch for c in cands))
        probe = await probe_once(addrs, intel.catch_all is None)
    else:
        first = await probe_once([c.address for c in batch[pilot_idx]], intel.catch_all is None)
        probe = first
        if first.catch_all is not True and first.session == SessionOutcome.ok:
            others = [i for i in range(len(pending)) if i != pilot_idx]
            narrowed = narrow_after_pilot(batch[pilot_idx], first, [batch[i] for i in others])
            if narrowed is not None:
                run.res.pilots += 1
                for i, cands in zip(others, narrowed, strict=True):
                    p, out, _, mode = pending[i]
                    pending[i] = (p, out, cands, mode)
            rest = list(
                dict.fromkeys(
                    c.address for i in others for c in pending[i][2] if c.address not in first.verdicts
                )
            )
            if rest:
                probe = merge_probes(first, await probe_once(rest, False))
    session_ms = sum(sessions[n_sessions:])
    if probe.catch_all is not None:
        mem.catch_all = intel.catch_all = probe.catch_all
        mem.catch_all_confidence = intel.catch_all_confidence = probe.catch_all_confidence
    state = await run.monitor.current_state(
        intel.provider, enabled=True
    )  # as the deep job does after a probe
    retry: list[tuple[BenchPerson, Outcome, list[Candidate], str]] = []
    expand: list[tuple[BenchPerson, Outcome, list[Candidate], str]] = []
    for p, out, cands, mode in pending:
        v, again = conclude_after_probe(
            [c.as_dict() for c in cands],
            intel,
            probe,
            smtp_state=state,
            attempt=attempt,
            max_attempts=MAX_DEEP_ATTEMPTS,
            first_name=p.first,
            last_name=p.last,
        )
        out.path, out.deep_attempts = "deep", attempt
        out.final_ms += session_ms
        _learn_from_probe(mem, cands, probe, state, p)
        if again:
            retry.append((p, out, cands, mode))
            continue
        if (v is None or v.status == EmailStatus.INVALID) and should_expand(cands, intel, probe, state):
            nxt = expand_candidates(
                p.first,
                p.last,
                intel,
                [c.address for c in cands],
                colleagues=[c for c in _everyone(d) if c != (p.first, p.last)],
                company_size_max=d.company_size_max,
                country=d.country,
            )
            if nxt:
                expand.append((p, out, nxt, mode))
                continue
        _apply(out, v, mode)
    if expand:
        run.res.expansions += len(expand)
        await _deep(run, d, intel, expand, sessions, attempt)  # same attempt: an expansion is not a retry
    if retry:
        # retries run later while other domains proceed: only the person's resolution time includes the wait
        wait = BACKOFF_S[min(attempt, len(BACKOFF_S)) - 1]
        for _, out, _, _ in retry:
            out.final_ms += wait * 1000
        await _deep(run, d, intel, retry, sessions, attempt + 1)


async def _engine_domain(run: _Run, d: BenchDomain, people: list[BenchPerson], wave: int) -> None:
    mem = run.memory.setdefault(d.domain, DomainMemory())
    cached = mem.built
    fast_ms = 0 if cached else run.lat.dns_ms + run.lat.profile_ms
    if not cached:
        run.res.dns_queries += 1
    mx = await run.verifier.resolve_mx(d.domain)
    intel = _intel(d, mx, mem)
    mem.built = True
    state = (
        (await run.monitor.gate(intel.provider, enabled=True)).state if run.smtp else SmtpHealthState.UNKNOWN
    )
    everyone = _everyone(d)
    pending: list[tuple[BenchPerson, Outcome, list[Candidate], str]] = []
    for i, p in enumerate(people):
        t0 = time.perf_counter()
        cands = build_candidates(
            p.first,
            p.last,
            intel,
            colleagues=[c for c in everyone if c != (p.first, p.last)],
            company_size_max=d.company_size_max,
            country=d.country,
        )
        verdicts = evaluate(cands, intel, smtp_state=state)
        best = pick(verdicts)
        cpu = (time.perf_counter() - t0) * 1000
        fast_ms += run.lat.person_ms
        out = Outcome(
            domain=d.domain,
            kind=d.kind,
            person=f"{p.first} {p.last}",
            truth=p.true_address,
            address=best.address if best is not None and best.status != EmailStatus.INVALID else None,
            judged=best.address if best is not None else None,
            status=best.status if best is not None else EmailStatus.UNKNOWN,
            confidence=best.confidence if best is not None else 0.0,
            resolver=best.resolver if best is not None else None,
            wave=wave,
            profile_cached=cached or i > 0,
            cpu_ms=cpu,
        )
        out.first_ms = out.final_ms = fast_ms
        run.outcomes.append(out)
        mode = deep_plan(best, intel, state, smtp_capable=True) if run.smtp else "none"
        if mode != "none":
            usable = [
                c
                for c, v in zip(cands, verdicts, strict=True)
                if not any(s.name == "affinity_guard" for s in v.signals)
            ]
            pending.append((p, out, deep_candidates(usable, mode, best), mode))
    sessions: list[int] = []
    if pending:
        for _, out, _, _ in pending:
            out.final_ms = fast_ms  # the batch starts once the domain's fast pass is done
        await _deep(run, d, intel, pending, sessions)
    run.res.chains.append((fast_ms, sessions))


# ---------------------------------------------------------------------------------------------------------------
# Legacy strategy
# ---------------------------------------------------------------------------------------------------------------


class _LegacyWorldVerifier:
    """Per-address verification through the world (one session per address), mapped to v1 semantics."""

    name = "legacy-world"

    def __init__(self, run: _Run, d: BenchDomain, mx: Any, sessions: list[int]) -> None:
        self.run, self.d, self.mx, self.sessions = run, d, mx, sessions

    async def verify(self, address: str) -> VerificationResult:
        run, mem = self.run, self.run.memory[self.d.domain]
        local, _ = split_address(address)
        res = VerificationResult(
            address=address,
            syntax_valid=True,
            mx_valid=bool(self.mx.accepts_mail),
            smtp_result=SmtpResult.not_attempted,
            catch_all=mem.legacy_catch_all,
            disposable=False,
            role_address=is_role_local_part(local),
            free_provider=False,
            verifier=self.name,
        )
        if not self.mx.accepts_mail:
            return res
        w0, r0 = run.world.simulated_latency_ms, run.world.rcpt_commands
        probe = await run.verifier.probe_domain(
            self.d.domain,
            list(self.mx.mx_hosts),
            [address],
            check_catch_all=mem.legacy_catch_all is None,
            random_probes=1,
        )
        self.sessions.append((run.world.simulated_latency_ms - w0) + run.lat.smtp_overhead_ms)
        run.res.smtp_sessions += 1
        run.res.smtp_rcpts += run.world.rcpt_commands - r0
        if probe.catch_all is not None:
            mem.legacy_catch_all = probe.catch_all
        res.catch_all = mem.legacy_catch_all
        rv = probe.verdicts.get(address)
        result = rv.result if rv is not None else SmtpResult.unknown
        # v1 had no "temporary": 4xx and timeouts were inconclusive and never retried
        res.smtp_result = SmtpResult.unknown if result == SmtpResult.temporary else result
        return res

    async def is_catch_all(self, domain: str) -> bool | None:
        return self.run.memory[self.d.domain].legacy_catch_all


async def _legacy_domain(run: _Run, d: BenchDomain, people: list[BenchPerson], wave: int) -> None:
    mem = run.memory.setdefault(d.domain, DomainMemory())
    cached = mem.built
    fast_ms = 0 if cached else run.lat.dns_ms
    if not cached:
        run.res.dns_queries += 1
    mem.built = True
    mx = await run.verifier.resolve_mx(d.domain)
    sessions: list[int] = []
    verifier = _LegacyWorldVerifier(run, d, mx, sessions)
    named = [(o.first, o.last, split_address(o.address)[0]) for o in d.observed if o.first and o.last]
    nameless = [split_address(o.address)[0] for o in d.observed if not (o.first and o.last)]
    for i, p in enumerate(people):
        known = [
            (pat, pattern_confidence(n, mem.successes[pat], mem.failures[pat]), n)
            for pat, n in (Counter(infer_patterns(named)) + mem.successes).items()
        ]
        n0 = len(sessions)
        t0 = time.perf_counter()
        finding = await find_email(
            first=p.first,
            last=p.last,
            domain=d.domain,
            published=[(o.address, o.source_url) for o in d.observed],
            known_patterns=sorted(known, key=lambda k: -k[1]),
            observed_local_parts=nameless,
            observed_samples=named,
            company_size_max=d.company_size_max,
            country=d.country,
            verifier=verifier,  # type: ignore[arg-type]
            max_probes=6,
        )
        cpu = (time.perf_counter() - t0) * 1000
        person_ms = sum(sessions[n0:])
        tried = finding.candidates_tried or []
        out = Outcome(
            domain=d.domain,
            kind=d.kind,
            person=f"{p.first} {p.last}",
            truth=p.true_address,
            address=finding.address,
            judged=finding.address or (tried[-1] if tried else None),
            status=finding.status,
            confidence=finding.overall_confidence,
            resolver=finding.method.value if finding.method else None,
            wave=wave,
            path="deep" if len(sessions) > n0 else "fast",
            first_ms=fast_ms + person_ms,
            final_ms=fast_ms + person_ms,
            deep_attempts=1 if len(sessions) > n0 else 0,
            profile_cached=cached or i > 0,
            cpu_ms=cpu,
        )
        run.outcomes.append(out)
        if (
            finding.status == EmailStatus.SAFE
            and finding.pattern
            and finding.method != EmailDiscoveryMethod.published
        ):
            mem.successes[finding.pattern] += 1  # v1 pattern memory: SMTP successes
    run.res.chains.append((fast_ms, sessions))


# ---------------------------------------------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------------------------------------------


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _pct(values: list[int], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(q * (len(s) - 1))))
    return float(s[k])


def _makespan_ms(chains: list[tuple[int, list[int]]], lat: LatencyModel) -> int:
    """List scheduling over two bounded pools: per-domain fast work, then its SMTP sessions."""
    fast = [0] * max(1, lat.fast_workers)
    smtp = [0] * max(1, lat.smtp_workers)
    heapq.heapify(fast)
    heapq.heapify(smtp)
    end_all = 0
    for fast_ms, sessions in chains:
        t = heapq.heappop(fast)
        end = t + fast_ms
        heapq.heappush(fast, end)
        for ms in sessions:
            s0 = max(heapq.heappop(smtp), end)
            end = s0 + ms
            heapq.heappush(smtp, end)
        end_all = max(end_all, end)
    return end_all


def _deliverable(d: BenchDomain, address: str | None) -> bool:
    return bool(address) and d.accepts_mail and (d.catch_all or address in d.mailboxes)


def metrics(run: _Run, *, cost: CostModel) -> dict[str, Any]:
    outs = run.outcomes
    by_domain = {d.domain: d for d in run.ds.domains}
    with_mailbox = [o for o in outs if o.truth]
    claimed = [o for o in outs if o.status in ACCEPTED and o.address]
    correct = [o for o in claimed if o.address == o.truth]
    safe = [o for o in outs if o.status == EmailStatus.SAFE and o.address]
    invalid = [o for o in outs if o.status == EmailStatus.INVALID and o.judged]
    invalid_fp = [o for o in invalid if _deliverable(by_domain[o.domain], o.judged)]
    any_correct = [o for o in outs if o.address and o.address == o.truth and o.status != EmailStatus.INVALID]

    # catch-all + pattern knowledge at the end of the run
    mail_domains = [d for d in run.ds.domains if d.accepts_mail]
    ca_known = ca_right = pat_known = pat_right = 0
    for d in mail_domains:
        mem = run.memory.get(d.domain)
        if mem is None:
            continue
        determined = mem.legacy_catch_all if run.strategy == "legacy" else mem.catch_all
        if determined is not None:
            ca_known += 1
            ca_right += determined == d.catch_all
        if run.strategy == "legacy":
            named = [(o.first, o.last, split_address(o.address)[0]) for o in d.observed if o.first and o.last]
            counts = Counter(infer_patterns(named)) + mem.successes
            dominant = counts.most_common(1)[0][0] if counts else None
        else:
            stats = learn(
                d.domain,
                _observed(d) + mem.verified,
                successes=dict(mem.successes),
                failures=dict(mem.failures),
                company_size_max=d.company_size_max,
                country=d.country,
            )
            dominant = stats[0].pattern if stats else None
        if dominant is not None and d.true_pattern:
            pat_known += 1
            pat_right += dominant == d.true_pattern

    first = [o.first_ms for o in outs]
    final = [o.final_ms for o in outs]
    makespan = _makespan_ms(run.res.chains, run.lat)
    resolved = len(claimed)
    cost_usd = (
        run.res.smtp_sessions * cost.smtp_session_usd
        + run.res.smtp_rcpts * cost.smtp_rcpt_usd
        + run.res.dns_queries * cost.dns_query_usd
    )
    deep = [o for o in outs if o.path == "deep"]
    return {
        "persons": len(outs),
        "persons_with_mailbox": len(with_mailbox),
        "email_discovery_recall": _rate(len(correct), len(with_mailbox)),
        "email_precision": _rate(len(correct), len(claimed)),
        "safe_precision": _rate(sum(o.address == o.truth for o in safe), len(safe)),
        "recall_any_status": _rate(len(any_correct), len(with_mailbox)),
        "invalid_verdicts": len(invalid),
        "invalid_false_positive_rate": _rate(len(invalid_fp), len(invalid)),
        "invalid_false_positives": len(invalid_fp),
        "catch_all_accuracy": _rate(ca_right, ca_known),
        "catch_all_coverage": _rate(ca_known, len(mail_domains)),
        "domain_pattern_accuracy": _rate(pat_right, pat_known),
        "domain_pattern_coverage": _rate(pat_known, sum(1 for d in mail_domains if d.true_pattern)),
        "avg_resolution_ms": round(statistics.fmean(final), 1) if final else None,
        "p50_resolution_ms": _pct(final, 0.5),
        "p95_resolution_ms": _pct(final, 0.95),
        "p50_first_verdict_ms": _pct(first, 0.5),
        "p95_first_verdict_ms": _pct(first, 0.95),
        "smtp_fallback_rate": _rate(len(deep), len(outs)),
        "smtp_sessions": run.res.smtp_sessions,
        "smtp_rcpts": run.res.smtp_rcpts,
        "expansion_rounds": run.res.expansions,
        "pilot_narrowings": run.res.pilots,
        "smtp_connections": run.world.connections,
        "rcpts_per_resolved_email": round(run.res.smtp_rcpts / resolved, 2) if resolved else None,
        "cache_hit_rate": _rate(sum(o.profile_cached for o in outs), len(outs)),
        "cost_usd": round(cost_usd, 6),
        "cost_per_email_usd": round(cost_usd / resolved, 7) if resolved else None,
        "makespan_ms": makespan,
        "emails_resolved_per_minute": round(resolved / (makespan / 60_000), 1)
        if makespan and resolved
        else None,
        "engine_cpu_ms_per_person": round(statistics.fmean(o.cpu_ms for o in outs), 3) if outs else None,
        "statuses": dict(Counter(o.status.value for o in outs)),
    }


def by_kind(run: _Run) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for kind in sorted({o.kind for o in run.outcomes}):
        outs = [o for o in run.outcomes if o.kind == kind]
        with_mailbox = [o for o in outs if o.truth]
        claimed = [o for o in outs if o.status in ACCEPTED and o.address]
        correct = [o for o in claimed if o.address == o.truth]
        out[kind] = {
            "persons": len(outs),
            "recall": _rate(len(correct), len(with_mailbox)),
            "precision": _rate(len(correct), len(claimed)),
            "statuses": dict(Counter(o.status.value for o in outs)),
        }
    return out


# ---------------------------------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------------------------------


async def run_strategy(
    ds: BenchDataset,
    strategy: str,
    *,
    waves: int = 2,
    lat: LatencyModel | None = None,
    cost: CostModel | None = None,
) -> dict[str, Any]:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r} (known: {', '.join(STRATEGIES)})")
    lat = lat or LatencyModel()
    cost = cost or CostModel()
    clock = _SimClock()
    world = build_world(ds, port25_blocked=strategy.endswith("_blocked"))
    monitor = SmtpHealthMonitor(MemoryHealthStore(), clock=clock, cache_ttl_s=0, enabled=lambda: True)
    verifier = WorldDeepVerifier(world, monitor=None if strategy == "legacy" else monitor)
    run = _Run(
        ds=ds, strategy=strategy, world=world, verifier=verifier, monitor=monitor, clock=clock, lat=lat
    )
    for wave in range(waves):
        for d in ds.domains:
            people = _waves(d.targets, waves)[wave]
            if not people:
                continue
            if strategy == "legacy":
                await _legacy_domain(run, d, people, wave)
            else:
                await _engine_domain(run, d, people, wave)
            clock.advance(1)
    return {
        "strategy": strategy,
        "description": STRATEGIES[strategy],
        "metrics": metrics(run, cost=cost),
        "by_kind": by_kind(run),
        "health": (await monitor.gate(None, enabled=True)).state.value if strategy != "legacy" else None,
    }


async def run_email_benchmark(
    *,
    domains: int = 300,
    seed: int = 7,
    waves: int = 2,
    strategies: list[str] | None = None,
    lat: LatencyModel | None = None,
    cost: CostModel | None = None,
) -> dict[str, Any]:
    ds = generate(domains, seed=seed)
    lat = lat or LatencyModel()
    cost = cost or CostModel()
    results = [
        await run_strategy(ds, s, waves=waves, lat=lat, cost=cost) for s in strategies or list(STRATEGIES)
    ]
    return {
        "dataset": {"domains": len(ds.domains), "targets": ds.targets, "seed": seed, "waves": waves},
        "latency_model": asdict(lat),
        "cost_model": asdict(cost),
        "strategies": {r["strategy"]: r for r in results},
        "disclaimer": "Synthetic, labelled scenarios against a simulated mail world: compares strategies and "
        "guards behaviour; it is not a precision claim about real-world domains.",
    }


TABLE_ROWS: list[tuple[str, str]] = [
    ("email_discovery_recall", "Discovery recall (SAFE+LIKELY_SAFE, correct)"),
    ("email_precision", "Precision (SAFE+LIKELY_SAFE)"),
    ("safe_precision", "SAFE precision"),
    ("recall_any_status", "Right address, any status"),
    ("invalid_false_positive_rate", "INVALID false-positive rate"),
    ("catch_all_accuracy", "Catch-all accuracy"),
    ("catch_all_coverage", "Catch-all known"),
    ("domain_pattern_accuracy", "Domain pattern accuracy"),
    ("avg_resolution_ms", "Avg resolution (ms, incl. retries)"),
    ("p50_resolution_ms", "P50 resolution (ms)"),
    ("p95_resolution_ms", "P95 resolution (ms)"),
    ("p95_first_verdict_ms", "P95 first verdict (ms)"),
    ("smtp_fallback_rate", "SMTP fallback rate"),
    ("smtp_sessions", "SMTP sessions (probe batches)"),
    ("smtp_connections", "SMTP connections"),
    ("smtp_rcpts", "RCPT commands"),
    ("cache_hit_rate", "Cache hit rate"),
    ("cost_per_email_usd", "Cost / email (assumed $)"),
    ("emails_resolved_per_minute", "Emails resolved / minute"),
    ("engine_cpu_ms_per_person", "Engine CPU ms / person"),
]


def format_table(report: dict[str, Any]) -> str:
    names = list(report["strategies"])
    width = max(len(label) for _, label in TABLE_ROWS) + 2
    lines = [
        f"{'':<{width}}" + "".join(f"{n:>20}" for n in names),
    ]
    for key, label in TABLE_ROWS:
        cells = []
        for n in names:
            v = report["strategies"][n]["metrics"].get(key)
            cells.append(f"{'—' if v is None else v:>20}")
        lines.append(f"{label:<{width}}" + "".join(cells))
    ds = report["dataset"]
    lines.append("")
    lines.append(
        f"{ds['domains']} domains, {ds['targets']} people, seed {ds['seed']}, {ds['waves']} waves. {report['disclaimer']}"
    )
    return "\n".join(lines)
