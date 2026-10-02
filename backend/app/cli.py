"""Command line interface.

    python -m app.cli doctor                      # check browser, engines, DNS, SMTP
    python -m app.cli run --target 1000           # full pipeline (resumable)
    python -m app.cli run --max-queries 30 --engines bing   # quick test run
    python -m app.cli test-site https://agency.com          # crawl + extract one site, print everything
    python -m app.cli export leads.csv --tier A,B
    python -m app.cli stats
    python -m app.cli serve                       # API on :8000
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import settings

app = typer.Typer(add_completion=False, help="LeadForge — agency lead scraping & enrichment engine")
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.command()
def run(
    target: int = typer.Option(settings.default_target_leads, help="Qualified leads wanted (discovery sizes its candidate pool from this)"),
    engines: str = typer.Option(",".join(settings.engines), help="Comma separated: google,bing,duckduckgo"),
    countries: str = typer.Option(",".join(settings.search_countries), help="Comma separated SERP countries"),
    max_queries: int | None = typer.Option(None, help="Cap the query matrix (quick tests)"),
    stages: str | None = typer.Option(None, help="Subset of discover,crawl,clients,founders,emails,finalize"),
    seeds: str | None = typer.Option(None, help="Comma separated extra domains"),
    min_score: int | None = typer.Option(None),
    stop_at_target: bool = typer.Option(False, help="Stop crawling once the target is reached (default: process everything)"),
    multiplier: float = typer.Option(8.0, help="Candidate pool size = target × multiplier"),
    reprocess: bool = typer.Option(False, help="Re-crawl every known candidate (pages come from cache) — use after improving the extractors"),
    name: str = typer.Option("cli run"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Run the pipeline (safe to interrupt with Ctrl+C and restart: nothing is refetched)."""
    _setup_logging(verbose)
    from sqlalchemy import update

    from .db import SessionLocal, init_db
    from .fetch.browser import get_browser
    from .fetch.http import get_fetcher
    from .models import Agency, Candidate, Run
    from .pipeline.orchestrator import STAGES, Pipeline, RunConfig

    cfg = RunConfig(
        target_leads=target, engines=[e.strip() for e in engines.split(",") if e.strip()], countries=[c.strip() for c in countries.split(",") if c.strip()],
        max_queries=max_queries, stages=[s.strip() for s in stages.split(",")] if stages else list(STAGES),
        seed_domains=[s.strip() for s in seeds.split(",")] if seeds else [], min_score=min_score, stop_at_target=stop_at_target,
        candidate_multiplier=multiplier, name=name,
    )

    async def _main() -> None:
        await init_db()
        async with SessionLocal() as s:
            if reprocess:
                await s.execute(update(Candidate).where(Candidate.status != "new").values(status="new", reject_reason=None))
                await s.execute(update(Agency).values(enrich_stage="none"))
                console.print("[yellow]reprocess: all candidates reset to 'new' (pages served from cache)[/yellow]")
            r = Run(name=cfg.name, status="pending", config=cfg.to_dict())
            s.add(r)
            await s.commit()
            run_id = r.id
        console.print(f"[bold]run #{run_id}[/bold] target={target} engines={cfg.engines} countries={cfg.countries} stages={cfg.stages}")
        pipeline = Pipeline(run_id, cfg)
        loop = asyncio.get_running_loop()
        try:
            import signal

            loop.add_signal_handler(signal.SIGINT, pipeline.cancel.set)
            loop.add_signal_handler(signal.SIGTERM, pipeline.cancel.set)
        except (NotImplementedError, RuntimeError):  # Windows
            pass
        try:
            await pipeline.run()
        finally:
            await get_browser().close()
            await get_fetcher().close()
        console.print(json.dumps(pipeline.stats, indent=2, default=str))

    asyncio.run(_main())


@app.command("test-site")
def test_site(url: str, verbose: bool = typer.Option(False, "-v"), json_out: bool = typer.Option(False, "--json")) -> None:
    """Crawl + extract + score one site and print everything (the fastest way to tune the extractors)."""
    _setup_logging(verbose)
    from .db import init_db
    from .fetch.browser import get_browser
    from .fetch.crawler import SiteCrawler
    from .fetch.http import get_fetcher
    from .pipeline.processor import process_site, summarize

    async def _main() -> None:
        await init_db()
        f = get_fetcher()
        await f.start()
        try:
            processed = await process_site(url, SiteCrawler(f))
        finally:
            await get_browser().close()
            await f.close()
        data = summarize(processed)
        if json_out:
            console.print_json(json.dumps(data, default=str))
            return
        console.rule(f"[bold]{data['domain']}[/bold] — {data['name']} ({data['country']})")
        console.print(f"alive={data['alive']} ({data['liveness']})  language={data['language']}  score={data['score']} status={data['status']} tier={data['tier']} reject={data['reject_reason']}")
        console.print("pages:", ", ".join(f"{k}" for k, _ in data["pages"]))
        console.print("emails:", data["emails"])
        console.print("socials:", data["socials"], "booking:", data["booking"])
        console.print("tech:", data["tech"])
        console.print("founder:", data["founder"])
        t = Table("client", "kind", "role", "conf", "website", "signals")
        for c in data["clients"]:
            t.add_row(c[0], c[1], str(c[2]), str(c[3]), str(c[4]), ",".join(c[5]))
        console.print(t)
        console.print("breakdown:", data["breakdown"])

    asyncio.run(_main())


@app.command()
def export(
    path: Path = typer.Argument(Path("leads.csv")),
    status: str = typer.Option("qualified,review"),
    tier: str | None = typer.Option(None, help="A,B,C"),
    min_score: int = typer.Option(0),
) -> None:
    """Export leads to CSV."""
    from .db import init_db
    from .export import export_csv

    async def _main() -> None:
        await init_db()
        n = await export_csv(path, statuses=tuple(status.split(",")), tiers=tuple(tier.split(",")) if tier else None, min_score=min_score)
        console.print(f"wrote {n} leads → {path}")

    asyncio.run(_main())


@app.command()
def stats() -> None:
    """Database counters."""
    from sqlalchemy import func, select

    from .db import SessionLocal, init_db
    from .models import Agency, AgencyEmail, Candidate, Client, Funnel, Run

    async def _main() -> None:
        await init_db()
        async with SessionLocal() as s:
            cands = (await s.execute(select(Candidate.status, func.count()).group_by(Candidate.status))).all()
            ags = (await s.execute(select(Agency.status, func.count()).group_by(Agency.status))).all()
            tiers = (await s.execute(select(Agency.tier, func.count()).where(Agency.status.in_(["qualified", "review"])).group_by(Agency.tier))).all()
            emails = (await s.execute(select(AgencyEmail.verification, func.count()).group_by(AgencyEmail.verification))).all()
            clients = (await s.execute(select(Client.status, func.count()).group_by(Client.status))).all()
            funnels = (await s.execute(select(func.count()).select_from(Funnel))).scalar_one()
            founders = (await s.execute(select(func.count()).where(Agency.founder_linkedin.isnot(None)))).scalar_one()
            runs = (await s.execute(select(Run).order_by(Run.id.desc()).limit(5))).scalars().all()
        t = Table("metric", "value")
        t.add_row("candidates", str(dict(cands)))
        t.add_row("agencies", str(dict(ags)))
        t.add_row("tiers", str(dict(tiers)))
        t.add_row("emails by verification", str(dict(emails)))
        t.add_row("clients", str(dict(clients)))
        t.add_row("funnels", str(funnels))
        t.add_row("founder linkedin", str(founders))
        console.print(t)
        for r in runs:
            console.print(f"run #{r.id} {r.status} stage={r.stage} {r.name} started={r.started_at} finished={r.finished_at}")

    asyncio.run(_main())


@app.command()
def doctor() -> None:
    """Check the environment: browser, search engines, DNS, outbound SMTP."""
    _setup_logging(False)
    from .db import init_db
    from .discovery.router import SearchRouter
    from .enrich.email_verify import get_verifier
    from .fetch.browser import get_browser
    from .fetch.http import get_fetcher

    async def _main() -> None:
        await init_db()
        f = get_fetcher()
        await f.start()
        console.print(f"data dir: {settings.data_dir}")
        b = get_browser()
        await b.start()
        console.print(f"browser: {'OK' if b.available else 'unavailable (set LF_BROWSER_EXECUTABLE or run: playwright install chromium)'}")
        r = SearchRouter(f, settings.engines)
        for name, p in r.providers.items():
            try:
                res = await r.search_one(p, "facebook ads agency for coaches", "us", 1)
                console.print(f"engine {name}: {len(res)} results {'OK' if res else 'EMPTY'} {p.health.snapshot()['last_error'] or ''}")
            except Exception as e:
                console.print(f"engine {name}: ERROR {e}")
        v = get_verifier()
        mx = await v.mx_hosts("gmail.com")
        console.print(f"dns MX gmail.com: {mx[:1]}")
        ok = await v._probe_port25()
        console.print(f"outbound SMTP port 25: {'open → mailbox verification enabled' if ok else 'blocked → MX-only verification'}")
        await b.close()
        await f.close()

    asyncio.run(_main())


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000, reload: bool = False) -> None:
    """Start the API server."""
    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
