"""Command line: `python -m scout.cli <command>`.

openapi <path>                 write the OpenAPI document (packages/schemas)
worker                         run a dedicated job worker process
seed --workspace <uuid>        load clearly-marked demo data into a workspace
migrate                        alembic upgrade head
benchmark suites|run|import    benchmark harness (see scout/benchmark/cli.py)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
import sys
import uuid
from pathlib import Path


def _openapi(path: str) -> None:
    from scout.main import create_app

    spec = create_app().openapi()
    Path(path).write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {path} ({len(spec.get('paths', {}))} paths)")


async def _worker() -> None:
    from scout.config import get_settings
    from scout.jobs.worker import Worker
    from scout.logging import configure_logging
    from scout.main import load_handlers

    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    load_handlers()
    w = Worker()
    await w.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await w.stop()


async def _seed(workspace: str, user: str | None) -> None:
    from scout.db.engine import session_scope
    from scout.seed import seed_workspace

    async with session_scope() as s:
        print(await seed_workspace(s, uuid.UUID(workspace), user))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="scout")
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("openapi")
    o.add_argument("path")
    sub.add_parser("worker")
    sd = sub.add_parser("seed")
    sd.add_argument("--workspace", required=True)
    sd.add_argument("--user")
    sub.add_parser("migrate")
    from scout.benchmark import cli as benchmark_cli

    benchmark_cli.add_parser(sub)
    args = p.parse_args(argv)
    if args.cmd == "openapi":
        _openapi(args.path)
    elif args.cmd == "worker":
        asyncio.run(_worker())
    elif args.cmd == "seed":
        asyncio.run(_seed(args.workspace, args.user))
    elif args.cmd == "migrate":
        sys.exit(subprocess.call([sys.executable, "-m", "alembic", "upgrade", "head"]))
    elif args.cmd == "benchmark":
        asyncio.run(benchmark_cli.run(args))


if __name__ == "__main__":
    main()
