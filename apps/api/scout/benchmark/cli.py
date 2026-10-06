"""``scout benchmark …`` commands (wired into ``scout.cli``).

suites [--json]                                     list registered suites
run --suite KEY [--strategy S …] [--config JSON]    run a synthetic suite in-process (no database)
run --dataset UUID --mode registry|live             run a dataset in-process (stored like an API run)
import --workspace UUID --file F.csv --name N       import a ground-truth CSV
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any


def add_parser(sub: Any) -> None:
    b = sub.add_parser("benchmark", help="benchmark harness (ground truth, suites)")
    bs = b.add_subparsers(dest="bench_cmd", required=True)
    ls = bs.add_parser("suites", help="list registered suites")
    ls.add_argument("--json", action="store_true")
    r = bs.add_parser("run", help="run a suite or a dataset")
    target = r.add_mutually_exclusive_group(required=True)
    target.add_argument("--suite")
    target.add_argument("--dataset")
    r.add_argument("--mode", choices=["registry", "live"], default="registry")
    r.add_argument("--strategy", action="append", help="suite strategy (repeatable); default: all")
    r.add_argument("--config", default="{}", help="JSON options (suite config or run config)")
    r.add_argument("--json", action="store_true")
    im = bs.add_parser("import", help="import a ground-truth CSV into a workspace")
    im.add_argument("--workspace", required=True)
    im.add_argument("--file", required=True)
    im.add_argument("--name", required=True)
    im.add_argument("--kind", choices=["leads", "email", "enrichment"], default="leads")
    im.add_argument("--company-input", choices=["domain", "name"], default="domain")
    im.add_argument("--partial-people", action="store_true", help="ground truth does not list every person")


def _fmt(m: dict[str, Any]) -> str:
    v = m.get("value")
    if v is None:
        return "—"
    unit = m.get("unit")
    if unit == "rate":
        return f"{v * 100:.1f}%"
    if unit == "ms":
        return f"{v:,.0f} ms"
    if unit == "usd":
        return f"${v:.4f}"
    if unit == "per_minute":
        return f"{v:.1f}/min"
    return f"{v:,}" if isinstance(v, int) else f"{v:.3f}"


def _table(metrics: dict[str, dict[str, Any]], title: str) -> str:
    lines = [title, "-" * len(title)]
    for m in metrics.values():
        ci = m.get("ci90")
        extra = f"  n={m['n']}" if m.get("n") is not None else ""
        if ci:
            extra += f"  90% CI [{ci[0] * 100:.1f}–{ci[1] * 100:.1f}%]"
        lines.append(f"{m.get('label', '?'):<32} {_fmt(m):>12}{extra}")
    return "\n".join(lines)


def _print_result(
    metrics: dict[str, Any], strategies: dict[str, Any] | None, notes: list[str], as_json: bool
) -> None:
    if as_json:
        print(
            json.dumps({"metrics": metrics, "strategies": strategies, "notes": notes}, indent=2, default=str)
        )
        return
    print(_table(metrics, "Metrics (measured; no comparative claim)"))
    for name, m in (strategies or {}).items():
        print()
        print(_table(m, f"Strategy: {name}"))
    for n in notes:
        print(f"note: {n}")


async def _suites(as_json: bool) -> None:
    from scout.benchmark.registry import list_suites, load_suites

    load_suites()
    rows = [s.public() for s in list_suites()]
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No suite registered.")
    for r in rows:
        strategies = ", ".join(r["strategies"]) or "—"
        print(
            f"{r['key']:<20} {r['title']}{'  [demo]' if r['demo'] else ''}\n{'':<20} strategies: {strategies}"
        )


async def _run_suite(key: str, strategies: list[str] | None, config: dict[str, Any], as_json: bool) -> None:
    from scout.benchmark.metrics import normalize_metrics
    from scout.benchmark.registry import load_suites, run_suite

    load_suites()
    if strategies:
        config = {**config, "strategies": strategies}
    try:
        result = await run_suite(key, config)
    except (KeyError, ValueError) as exc:
        sys.exit(str(exc))
    labels, definitions = result.labels, result.definitions
    metrics = normalize_metrics(result.metrics, result.samples, labels=labels, definitions=definitions)
    strat = (
        {
            k: normalize_metrics(v, result.samples, labels=labels, definitions=definitions)
            for k, v in result.strategies.items()
        }
        if result.strategies
        else None
    )
    _print_result(metrics, strat, result.notes, as_json)


async def _run_dataset(dataset: str, mode: str, config: dict[str, Any], as_json: bool) -> None:
    from scout.benchmark import runner
    from scout.db.benchmark_models import BenchmarkDataset, BenchmarkRun
    from scout.db.engine import session_scope
    from scout.main import load_handlers
    from scout.services import benchmark as svc

    load_handlers()
    async with session_scope() as s:
        d = await s.get(BenchmarkDataset, uuid.UUID(dataset))
        if d is None:
            sys.exit(f"dataset {dataset} not found")
        run = await svc.start_run(
            s,
            d.workspace_id,
            mode=mode,
            dataset_id=d.id,
            suite=None,
            strategy=None,
            config=config,
            user_id=None,
            enqueue=False,  # executed here, not by a worker
        )
        run_id = run.id
    state = await runner.execute(run_id, slice_s=None)
    async with session_scope() as s:
        r = await s.get(BenchmarkRun, run_id)
        assert r is not None
        print(
            f"run {run_id}: {state} · {r.items_done} items · ${float(r.cost_usd or 0):.4f}", file=sys.stderr
        )
        _print_result(r.metrics or {}, None, list(r.notes or []), as_json)


async def _import(args: argparse.Namespace) -> None:
    from scout.db.engine import session_scope
    from scout.services import benchmark as svc

    text = await asyncio.to_thread(Path(args.file).read_text, encoding="utf-8-sig")
    parsed = svc.parse_import(
        kind=args.kind,
        csv=text,
        items=None,
        people_exhaustive=not args.partial_people,
        company_input=args.company_input,
    )
    async with session_scope() as s:
        d = await svc.create_dataset(
            s,
            uuid.UUID(args.workspace),
            name=args.name,
            kind=args.kind,
            description=None,
            parsed=parsed,
            people_exhaustive=not args.partial_people,
            company_input=args.company_input,
            columns=None,
            source="csv",
            user_id=None,
        )
        await s.flush()
        print(f"dataset {d.id}: {d.item_count} items")
        for w in parsed.warnings:
            print(f"warning: {w}")


async def run(args: argparse.Namespace) -> None:
    from scout.errors import AppError

    try:
        if args.bench_cmd == "suites":
            await _suites(args.json)
        elif args.bench_cmd == "run":
            config = json.loads(args.config or "{}")
            if args.suite:
                await _run_suite(args.suite, args.strategy, config, args.json)
            else:
                await _run_dataset(args.dataset, args.mode, config, args.json)
        elif args.bench_cmd == "import":
            await _import(args)
    except AppError as exc:
        details = exc.extra.get("errors") if exc.extra else None
        for e in (details or [])[:20]:
            print(f"  {e.get('where')}: {e.get('field')}: {e.get('message')}", file=sys.stderr)
        sys.exit(f"error: {exc.message}")
    finally:
        from scout.db.engine import dispose_engine

        await dispose_engine()
