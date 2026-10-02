"""Resumable multi-stage pipeline.

    discover  → candidates (SERPs × engines × countries, listicle expansion, directories, seeds)
    crawl     → agencies   (crawl + extract + qualify every new candidate)
    clients   → client websites + funnels
    founders  → founder LinkedIn via search engines
    emails    → MX / SMTP verification, pattern guessing
    finalize  → tiers, stats, CSV export

Every stage is idempotent: state lives in SQLite, SERPs and pages are cached, so a run can be stopped and
restarted at any time without redoing work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import SessionLocal
from ..discovery.directories import seed_files, teachable_experts
from ..discovery.listicle import extract_listicle_links, is_listicle, looks_like_agency_list
from ..discovery.queries import build_queries
from ..discovery.router import SearchRouter
from ..enrich.client_resolver import resolve_client
from ..enrich.email_verify import get_verifier
from ..enrich.founder import FounderHit, resolve_linkedin
from ..export import export_csv
from ..fetch.browser import get_browser
from ..fetch.crawler import SiteCrawler
from ..fetch.http import get_fetcher
from ..models import Agency, AgencyEmail, Candidate, Client, Event, Funnel, Run
from ..util.text import parse_html
from ..util.urls import canonicalize, ensure_scheme, host_of, is_blocked_domain, registrable_domain, social_network
from .processor import persist_agency, process_site

log = logging.getLogger(__name__)

STAGES = ["discover", "crawl", "clients", "founders", "emails", "finalize"]


@dataclass
class RunConfig:
    target_leads: int = 1000
    engines: list[str] = field(default_factory=lambda: list(settings.engines))
    countries: list[str] = field(default_factory=lambda: list(settings.search_countries))
    max_queries: int | None = None
    stages: list[str] = field(default_factory=lambda: list(STAGES))
    seed_domains: list[str] = field(default_factory=list)
    extra_queries: list[str] = field(default_factory=list)
    min_score: int | None = None
    resume: bool = True
    candidate_multiplier: float = 8.0  # discovery stops once candidates ≥ target × multiplier
    stop_at_target: bool = False  # crawl stops once qualified ≥ target (default: process everything → max volume)
    max_clients_per_agency: int = 4
    name: str = "run"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunConfig:
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__ and v is not None}
        return cls(**known)


class Pipeline:
    def __init__(self, run_id: int, config: RunConfig, cancel: asyncio.Event | None = None) -> None:
        self.run_id = run_id
        self.cfg = config
        self.cancel = cancel or asyncio.Event()
        self.fetcher = get_fetcher()
        self.browser = get_browser()
        self.crawler = SiteCrawler(self.fetcher)
        self.router = SearchRouter(self.fetcher, config.engines)
        self.verifier = get_verifier()
        self.stats: dict[str, Any] = {"started": datetime.utcnow().isoformat(timespec="seconds")}
        self._last_flush = 0.0

    # --- bookkeeping -------------------------------------------------------------------------------------------
    async def event(self, message: str, *, level: str = "info", stage: str = "", data: dict | None = None) -> None:
        getattr(log, level if level in ("debug", "info", "warning", "error") else "info")("[run %s][%s] %s", self.run_id, stage, message)
        async with SessionLocal() as s:
            s.add(Event(run_id=self.run_id, level=level, stage=stage, message=message[:2000], data=data))
            await s.commit()

    async def set_stage(self, stage: str) -> None:
        async with SessionLocal() as s:
            await s.execute(update(Run).where(Run.id == self.run_id).values(stage=stage, updated_at=datetime.utcnow()))
            await s.commit()

    async def flush_stats(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_flush < 3:
            return
        self._last_flush = time.monotonic()
        self.stats["engines"] = self.router.health_snapshot()
        self.stats["fetch"] = dict(self.fetcher.stats)
        self.stats["browser"] = dict(self.browser.stats)
        self.stats["updated"] = datetime.utcnow().isoformat(timespec="seconds")
        async with SessionLocal() as s:
            await s.execute(update(Run).where(Run.id == self.run_id).values(stats=self.stats, updated_at=datetime.utcnow()))
            await s.commit()

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    async def counts(self) -> dict[str, int]:
        async with SessionLocal() as s:
            cands = (await s.execute(select(Candidate.status, func.count()).group_by(Candidate.status))).all()
            ags = (await s.execute(select(Agency.status, func.count()).group_by(Agency.status))).all()
        out = {f"candidates_{k}": v for k, v in cands}
        out.update({f"agencies_{k}": v for k, v in ags})
        out["candidates_total"] = sum(v for _, v in cands)
        out["agencies_total"] = sum(v for _, v in ags)
        return out

    # --- stage 1: discovery -------------------------------------------------------------------------------------
    async def _add_candidates(self, items: list[tuple[str, str, str | None, str | None, str | None]]) -> int:
        """items: (url, source, query, title, snippet). Returns number of *new* domains."""
        if not items:
            return 0
        rows: dict[str, dict] = {}
        for url, source, query, title, snippet in items:
            url = ensure_scheme(url)
            dom = registrable_domain(url)
            host = host_of(url)
            if not dom or "." not in dom or is_blocked_domain(dom) or social_network(url):
                continue
            if any(host.startswith(p) for p in ("mail.", "cdn.", "static.", "api.", "app.", "docs.", "help.", "support.", "my.", "login.", "shop.", "store.")):
                continue
            home = f"https://{host}/"
            cur = rows.get(dom)
            if cur is None:
                rows[dom] = {"domain": dom, "url": home, "source": source, "query": query, "title": (title or "")[:300], "snippet": (snippet or "")[:500], "run_id": self.run_id}
        if not rows:
            return 0
        async with SessionLocal() as s:
            existing = set((await s.execute(select(Candidate.domain).where(Candidate.domain.in_(list(rows))))).scalars())
            stmt = sqlite_insert(Candidate).values(list(rows.values()))
            stmt = stmt.on_conflict_do_update(index_elements=["domain"], set_={"hits": Candidate.hits + 1})
            await s.execute(stmt)
            await s.commit()
        return len(rows) - len(existing)

    async def _expand_listicle(self, url: str, query: str) -> int:
        res = await self.fetcher.fetch(url, ttl_hours=24 * 14, retries=1)
        if not res.ok:
            return 0
        page = parse_html(res.html, res.final_url or url)
        if page.text_len < 1500 and self.browser.available:
            html, final, _ = await self.browser.render(res.final_url or url)
            if html:
                page = parse_html(html, final)
        if not looks_like_agency_list(page):
            return 0
        links = [l for l in extract_listicle_links(page, registrable_domain(url)) if l.score >= 1.0][:60]
        n = await self._add_candidates([(l.url, "listicle", query, l.anchor, f"from {url}") for l in links])
        self.stats["listicles_expanded"] = self.stats.get("listicles_expanded", 0) + 1
        self.stats["listicle_candidates"] = self.stats.get("listicle_candidates", 0) + n
        return n

    async def stage_discover(self) -> None:
        cfg = self.cfg
        pool_target = int(cfg.target_leads * cfg.candidate_multiplier)
        # seeds first: cheap and high quality
        seeds: list[tuple[str, str, str | None, str | None, str | None]] = [(d, "seed", None, None, None) for d in cfg.seed_domains]
        seeds += [(u, src, None, None, None) for u, src in seed_files()]
        try:
            seeds += [(u, "directory:teachable", None, label, None) for u, label in await teachable_experts(self.fetcher)]
        except Exception as e:  # pragma: no cover - network
            await self.event(f"teachable experts failed: {e}", level="warning", stage="discover")
        n = await self._add_candidates(seeds)
        await self.event(f"seeds: {len(seeds)} urls, {n} new candidates", stage="discover")

        queries = build_queries(cfg.extra_queries)
        if cfg.max_queries:
            queries = queries[: cfg.max_queries]
        self.stats["queries_total"] = len(queries) * len(cfg.countries)
        self.stats["queries_done"] = 0
        self.stats["serp_results"] = 0
        self.stats["new_candidates"] = n
        await self.event(f"discovery: {len(queries)} queries × {cfg.countries} countries × engines {list(self.router.providers)}", stage="discover")

        listicle_seen: set[str] = set()
        sem = asyncio.Semaphore(settings.search_concurrency)
        lock = asyncio.Lock()

        async def one(q, country: str) -> None:
            if self.cancelled():
                return
            async with sem:
                results = await self.router.search(q.text, country=country)
            items = []
            listicles = []
            for r in results:
                dom = registrable_domain(r.url)
                if not dom or is_blocked_domain(dom):
                    continue
                if is_listicle(r.title, r.snippet, r.url):
                    key = canonicalize(r.url)
                    if key not in listicle_seen:
                        listicle_seen.add(key)
                        listicles.append(r)
                    continue
                items.append((r.url, f"serp:{r.engine}", q.text, r.title, r.snippet))
            new = await self._add_candidates(items)
            for r in listicles[:6]:
                try:
                    new += await self._expand_listicle(r.url, q.text)
                except Exception as e:  # pragma: no cover - network
                    log.debug("listicle expansion failed %s: %s", r.url, e)
            async with lock:
                self.stats["queries_done"] += 1
                self.stats["serp_results"] += len(results)
                self.stats["new_candidates"] += new
            await self.flush_stats()

        # country-major order: every query in the primary country first, then widen
        for country in cfg.countries:
            if self.cancelled():
                break
            counts = await self.counts()
            if counts["candidates_total"] >= pool_target:
                await self.event(f"candidate pool reached {counts['candidates_total']} ≥ {pool_target}; stopping discovery", stage="discover")
                break
            if not self.router.any_alive():
                await self.event("every search engine is blocked/disabled; stopping discovery", level="warning", stage="discover")
                break
            batch = 24
            for i in range(0, len(queries), batch):
                if self.cancelled() or not self.router.any_alive():
                    break
                await asyncio.gather(*(one(q, country) for q in queries[i: i + batch]))
                counts = await self.counts()
                self.stats.update(counts)
                if counts["candidates_total"] >= pool_target:
                    break
                if all(p.health.disabled or not p.health.available for p in self.router.providers.values()):
                    # all engines cooling down: wait for the shortest cooldown instead of burning queries
                    wait = min(p.health.cooldown_until for p in self.router.providers.values() if not p.health.disabled) if self.router.any_alive() else 0
                    delay = max(0.0, wait - time.monotonic())
                    if delay > 0:
                        await self.event(f"all engines cooling down; waiting {int(delay)}s", level="warning", stage="discover")
                        try:
                            await asyncio.wait_for(self.cancel.wait(), timeout=delay)
                        except TimeoutError:
                            pass
        self.stats.update(await self.counts())
        await self.flush_stats(force=True)
        await self.event(f"discovery done: {self.stats.get('queries_done')} queries, {self.stats.get('serp_results')} results, {self.stats.get('new_candidates')} new candidates", stage="discover")

    # --- stage 2: crawl + qualify -------------------------------------------------------------------------------
    async def stage_crawl(self) -> None:
        cfg = self.cfg
        statuses = ["new"] if cfg.resume else ["new", "error", "crawling"]
        statuses.append("crawling")  # interrupted runs
        site_conc = max(4, settings.fetch_concurrency // 3)
        sem = asyncio.Semaphore(site_conc)
        self.stats.setdefault("crawled", 0)
        self.stats.setdefault("qualified", 0)
        self.stats.setdefault("review", 0)
        self.stats.setdefault("rejected", 0)
        self.stats.setdefault("crawl_errors", 0)
        reasons: dict[str, int] = self.stats.setdefault("reject_reasons", {})
        lock = asyncio.Lock()

        async def one(cand_id: int, url: str) -> None:
            if self.cancelled():
                return
            async with sem:
                try:
                    processed = await process_site(url, self.crawler, min_score=cfg.min_score)
                    sc = processed.score
                    async with SessionLocal() as s:
                        cand = await s.get(Candidate, cand_id)
                        if cand is None:
                            return
                        if sc is not None and (sc.status != "rejected" or processed.liveness.alive):
                            await persist_agency(s, processed, candidate_id=cand.id, run_id=self.run_id)
                        cand.status = "qualified" if sc and sc.status == "qualified" else "review" if sc and sc.status == "review" else "rejected"
                        cand.reject_reason = (sc.reject_reason if sc else "unknown")
                        cand.processed_at = datetime.utcnow()
                        await s.commit()
                    async with lock:
                        self.stats["crawled"] += 1
                        key = sc.status if sc else "rejected"
                        self.stats[key] = self.stats.get(key, 0) + 1
                        if sc and sc.reject_reason:
                            r = sc.reject_reason.split(":")[0]
                            reasons[r] = reasons.get(r, 0) + 1
                except Exception as e:
                    log.exception("crawl failed for %s", url)
                    async with SessionLocal() as s:
                        cand = await s.get(Candidate, cand_id)
                        if cand is not None:
                            cand.status = "error"
                            cand.reject_reason = f"{type(e).__name__}: {str(e)[:200]}"
                            cand.processed_at = datetime.utcnow()
                            await s.commit()
                    async with lock:
                        self.stats["crawl_errors"] += 1
            await self.flush_stats()

        while not self.cancelled():
            async with SessionLocal() as s:
                rows = (await s.execute(
                    select(Candidate.id, Candidate.url).where(Candidate.status.in_(statuses)).order_by(Candidate.hits.desc(), Candidate.id).limit(settings.worker_batch * 4)
                )).all()
                if rows:
                    await s.execute(update(Candidate).where(Candidate.id.in_([r[0] for r in rows])).values(status="crawling"))
                    await s.commit()
            if not rows:
                break
            statuses = ["new"]  # only the first batch picks up interrupted ones
            await asyncio.gather(*(one(cid, url) for cid, url in rows))
            self.stats.update(await self.counts())
            await self.flush_stats(force=True)
            if cfg.stop_at_target and self.stats.get("agencies_qualified", 0) >= cfg.target_leads:
                await self.event(f"target reached: {self.stats['agencies_qualified']} qualified agencies", stage="crawl")
                break
        if self.cancelled():
            async with SessionLocal() as s:  # release the batch we had claimed
                await s.execute(update(Candidate).where(Candidate.status == "crawling").values(status="new"))
                await s.commit()
        await self.event(f"crawl done: {self.stats.get('crawled')} sites, qualified={self.stats.get('qualified')}, review={self.stats.get('review')}, rejected={self.stats.get('rejected')}, errors={self.stats.get('crawl_errors')}", stage="crawl")

    # --- stage 3: clients → websites + funnels ------------------------------------------------------------------
    async def stage_clients(self) -> None:
        sem = asyncio.Semaphore(6)
        self.stats.setdefault("clients_resolved", 0)
        self.stats.setdefault("funnels_found", 0)
        self.stats.setdefault("clients_checked", 0)
        lock = asyncio.Lock()
        router = self.router if self.router.any_alive() else None

        async def one(agency_id: int) -> None:
            if self.cancelled():
                return
            async with sem:
                async with SessionLocal() as s:
                    agency = await s.get(Agency, agency_id, options=[selectinload(Agency.clients).selectinload(Client.funnels)])
                    if agency is None:
                        return
                    todo = [c for c in sorted(agency.clients, key=lambda c: -c.confidence) if c.status == "found"][: self.cfg.max_clients_per_agency]
                    agency.enrich_stage = "clients"
                    await s.commit()
                    todo_data = [(c.id, c.name, c.kind, c.role_title, c.website) for c in todo]
                for cid, name, kind, role, website in todo_data:
                    if self.cancelled():
                        return
                    try:
                        rc = await resolve_client(name, kind, role, website, router, self.crawler)
                    except Exception as e:
                        log.debug("client resolve failed %s: %s", name, e)
                        continue
                    async with SessionLocal() as s:
                        client = await s.get(Client, cid)
                        if client is None:
                            continue
                        client.status = rc.status
                        if rc.website:
                            client.website = rc.website
                            client.website_source = rc.website_source
                        for f in rc.funnels:
                            s.add(Funnel(client_id=client.id, url=f.url, funnel_type=f.funnel_type, platform=f.platform, offer=f.offer, price_hint=f.price_hint, steps=f.steps, evidence=f.evidence, confidence=round(f.confidence, 3)))
                        await s.commit()
                    async with lock:
                        self.stats["clients_checked"] += 1
                        if rc.status == "resolved":
                            self.stats["clients_resolved"] += 1
                        self.stats["funnels_found"] += len(rc.funnels)
                async with SessionLocal() as s:
                    await s.execute(update(Agency).where(Agency.id == agency_id).values(enrich_stage="clients_done"))
                    await s.commit()
            await self.flush_stats()

        async with SessionLocal() as s:
            ids = list((await s.execute(
                select(Agency.id).where(Agency.status.in_(["qualified", "review"]), Agency.enrich_stage.in_(["none", "clients"]))
                .order_by(Agency.score.desc())
            )).scalars())
        await self.event(f"resolving clients for {len(ids)} agencies", stage="clients")
        for i in range(0, len(ids), 24):
            if self.cancelled():
                break
            await asyncio.gather(*(one(a) for a in ids[i: i + 24]))
        await self.flush_stats(force=True)
        await self.event(f"clients done: {self.stats.get('clients_resolved')} resolved / {self.stats.get('clients_checked')} checked, {self.stats.get('funnels_found')} funnels", stage="clients")

    # --- stage 4: founders --------------------------------------------------------------------------------------
    async def stage_founders(self) -> None:
        if not self.router.any_alive():
            await self.event("no search engine available: skipping founder LinkedIn resolution", level="warning", stage="founders")
            return
        self.stats.setdefault("founders_resolved", 0)
        self.stats.setdefault("founders_checked", 0)
        sem = asyncio.Semaphore(2)

        async def one(agency_id: int, name: str, domain: str, fname: str | None, ftitle: str | None, fconf: float | None) -> None:
            if self.cancelled() or not self.router.any_alive():
                return
            async with sem:
                hit = FounderHit(name=fname, title=ftitle, confidence=fconf or 0.0)
                try:
                    hit = await resolve_linkedin(self.router, name, hit, domain)
                except Exception as e:
                    log.debug("founder resolve failed %s: %s", domain, e)
                async with SessionLocal() as s:
                    vals = {"enrich_stage": "founders_done"}
                    if hit.linkedin:
                        vals.update(founder_name=hit.name, founder_title=hit.title, founder_linkedin=hit.linkedin, founder_source=hit.source, founder_confidence=hit.confidence)
                    await s.execute(update(Agency).where(Agency.id == agency_id).values(**vals))
                    await s.commit()
                self.stats["founders_checked"] += 1
                if hit.linkedin:
                    self.stats["founders_resolved"] += 1
            await self.flush_stats()

        async with SessionLocal() as s:
            rows = (await s.execute(
                select(Agency.id, Agency.name, Agency.domain, Agency.founder_name, Agency.founder_title, Agency.founder_confidence)
                .where(Agency.status.in_(["qualified", "review"]), Agency.founder_linkedin.is_(None), Agency.enrich_stage != "founders_done")
                .order_by(Agency.score.desc())
            )).all()
        await self.event(f"resolving founder LinkedIn for {len(rows)} agencies", stage="founders")
        for i in range(0, len(rows), 8):
            if self.cancelled():
                break
            await asyncio.gather(*(one(*r) for r in rows[i: i + 8]))
        await self.flush_stats(force=True)
        await self.event(f"founders done: {self.stats.get('founders_resolved')} / {self.stats.get('founders_checked')}", stage="founders")

    # --- stage 5: emails ----------------------------------------------------------------------------------------
    async def stage_emails(self) -> None:
        self.stats.setdefault("emails_verified", 0)
        self.stats.setdefault("emails_guessed", 0)
        sem = asyncio.Semaphore(8)

        async def one(agency_id: int, domain: str) -> None:
            if self.cancelled():
                return
            async with sem:
                async with SessionLocal() as s:
                    emails = list((await s.execute(select(AgencyEmail).where(AgencyEmail.agency_id == agency_id).order_by(AgencyEmail.rank))).scalars())
                    todo = [(e.id, e.email) for e in emails if e.verification == "unknown"]
                results = []
                for eid, email in todo:
                    try:
                        results.append((eid, await self.verifier.verify(email)))
                    except Exception as e:
                        log.debug("verify failed %s: %s", email, e)
                guessed = []
                if not emails and settings.guess_email_patterns:
                    try:
                        guessed = await self.verifier.guess(domain)
                    except Exception as e:
                        log.debug("guess failed %s: %s", domain, e)
                async with SessionLocal() as s:
                    for eid, r in results:
                        row = await s.get(AgencyEmail, eid)
                        if row is not None:
                            row.verification = r.status
                            row.verified_at = datetime.utcnow()
                    for i, g in enumerate(guessed[:2]):
                        s.add(AgencyEmail(agency_id=agency_id, email=g.email, source="guessed", verification=g.status, confidence=0.35 if g.status == "smtp_valid" else 0.2, rank=100 + i, is_primary=(i == 0), verified_at=datetime.utcnow()))
                    # primary = best verified, same-domain first
                    rows = list((await s.execute(select(AgencyEmail).where(AgencyEmail.agency_id == agency_id))).scalars())
                    order = {"smtp_valid": 0, "catch_all": 1, "mx_valid": 2, "unknown": 3, "no_mx": 5, "invalid": 6}
                    rows.sort(key=lambda e: (e.source == "guessed", order.get(e.verification, 4), not e.email.endswith("@" + domain), e.rank))
                    for i, e in enumerate(rows):
                        e.is_primary = i == 0 and e.verification not in ("invalid", "no_mx")
                    await s.execute(update(Agency).where(Agency.id == agency_id).values(enrich_stage="emails_done"))
                    await s.commit()
                self.stats["emails_verified"] += len(results)
                self.stats["emails_guessed"] += len(guessed[:2])
            await self.flush_stats()

        async with SessionLocal() as s:
            rows = (await s.execute(select(Agency.id, Agency.domain).where(Agency.status.in_(["qualified", "review"]), Agency.enrich_stage != "emails_done").order_by(Agency.score.desc()))).all()
        await self.event(f"verifying emails for {len(rows)} agencies (smtp={'auto' if settings.smtp_verify else 'off'})", stage="emails")
        for i in range(0, len(rows), 32):
            if self.cancelled():
                break
            await asyncio.gather(*(one(a, d) for a, d in rows[i: i + 32]))
        await self.flush_stats(force=True)
        await self.event(f"emails done: {self.stats.get('emails_verified')} verified, {self.stats.get('emails_guessed')} guessed (smtp available: {self.verifier.smtp_available})", stage="emails")

    # --- stage 6: finalize --------------------------------------------------------------------------------------
    async def stage_finalize(self) -> None:
        async with SessionLocal() as s:
            agencies = list((await s.execute(
                select(Agency).where(Agency.status.in_(["qualified", "review"])).options(selectinload(Agency.emails), selectinload(Agency.clients).selectinload(Client.funnels))
            )).scalars())
            tiers = {"A": 0, "B": 0, "C": 0}
            for a in agencies:
                good_emails = [e for e in a.emails if e.verification not in ("invalid", "no_mx") and (e.source != "guessed" or e.verification == "smtp_valid")]
                has_email = bool(good_emails)
                coach_client = any(c.confidence >= 0.5 and (c.role_title or c.status == "resolved") for c in a.clients)
                has_funnel = any(c.funnels for c in a.clients)
                min_score = self.cfg.min_score or settings.min_lead_score
                if a.status == "qualified" or (a.score >= min_score and not a.reject_reason):
                    if has_email and coach_client and has_funnel:
                        a.tier = "A"
                    elif has_email and coach_client:
                        a.tier = "A" if a.score >= min_score + 10 else "B"
                    elif has_email:
                        a.tier = "B"
                    else:
                        a.tier = "C"
                    a.status = "qualified" if a.tier in ("A", "B") else "review"
                else:
                    a.tier = "C"
                tiers[a.tier] = tiers.get(a.tier, 0) + 1
            await s.commit()
        self.stats["tiers"] = tiers
        self.stats.update(await self.counts())
        path = settings.data_dir / "exports" / f"run_{self.run_id}_{datetime.utcnow():%Y%m%d_%H%M%S}.csv"
        n = await export_csv(path)
        self.stats["export"] = {"path": str(path), "rows": n}
        await self.flush_stats(force=True)
        await self.event(f"finalize: tiers {tiers}; exported {n} rows to {path}", stage="finalize")

    # --- driver ---------------------------------------------------------------------------------------------------
    async def run(self) -> None:
        await self.fetcher.start()
        async with SessionLocal() as s:
            await s.execute(update(Run).where(Run.id == self.run_id).values(status="running", started_at=datetime.utcnow(), config=self.cfg.to_dict()))
            await s.commit()
        try:
            for stage in STAGES:
                if stage not in self.cfg.stages:
                    continue
                if self.cancelled():
                    break
                await self.set_stage(stage)
                await self.event(f"stage {stage} started", stage=stage)
                try:
                    await getattr(self, f"stage_{stage}")()
                except Exception as e:
                    log.exception("stage %s failed", stage)
                    await self.event(f"stage {stage} failed: {type(e).__name__}: {e}", level="error", stage=stage)
                    if stage in ("discover", "crawl"):
                        raise
            status = "cancelled" if self.cancelled() else "completed"
            async with SessionLocal() as s:
                await s.execute(update(Run).where(Run.id == self.run_id).values(status=status, finished_at=datetime.utcnow(), stage="done" if status == "completed" else "cancelled", stats=self.stats))
                await s.commit()
            await self.event(f"run {status}", stage="done")
        except Exception as e:
            async with SessionLocal() as s:
                await s.execute(update(Run).where(Run.id == self.run_id).values(status="failed", finished_at=datetime.utcnow(), error=f"{type(e).__name__}: {e}", stats=self.stats))
                await s.commit()
            raise
        finally:
            try:
                await self.browser.close()
            except Exception:
                pass


class RunManager:
    """Owns the background task of the active run (one run at a time per process)."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.run_id: int | None = None
        self.cancel = asyncio.Event()

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self, cfg: RunConfig) -> int:
        if self.active:
            raise RuntimeError(f"run {self.run_id} is still active")
        async with SessionLocal() as s:
            run = Run(name=cfg.name, status="pending", config=cfg.to_dict())
            s.add(run)
            await s.commit()
            run_id = run.id
        self.cancel = asyncio.Event()
        self.run_id = run_id
        pipeline = Pipeline(run_id, cfg, self.cancel)
        self.task = asyncio.create_task(pipeline.run(), name=f"run-{run_id}")
        return run_id

    async def stop(self) -> None:
        if self.active:
            self.cancel.set()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=120)
            except (TimeoutError, Exception):
                pass


manager = RunManager()
