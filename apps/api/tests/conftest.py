"""Shared test fixtures.

Integration tests use a dedicated Postgres database (TEST_DATABASE_URL, default scout_test on localhost),
migrated once per session with Alembic, and truncated between tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[1]
TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/scout_test")

os.environ.setdefault("APP_ENV", "test")
os.environ["DATABASE_URL"] = TEST_DB
os.environ.setdefault("AI_PROVIDER", "local")
os.environ.setdefault("WORKER_ENABLED", "false")
os.environ.setdefault("VERIFIER_BACKEND", "builtin")
os.environ.setdefault("PER_DOMAIN_DELAY_MS", "0")


def _db_available() -> bool:
    import asyncio

    import asyncpg

    async def _check() -> bool:
        try:
            conn = await asyncpg.connect(TEST_DB.replace("postgresql+asyncpg://", "postgresql://"), timeout=3)
            await conn.close()
            return True
        except Exception:
            return False

    return asyncio.run(_check())


_DB_OK: bool | None = None


def db_ok() -> bool:
    global _DB_OK
    if _DB_OK is None:
        _DB_OK = _db_available()
    return _DB_OK


@pytest.fixture(scope="session")
def migrated_db() -> str:
    if not db_ok():
        pytest.skip("Postgres test database not available")
    env = {**os.environ, "DATABASE_URL": TEST_DB}
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"], cwd=API_DIR, env=env, check=False, capture_output=True
    )
    res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API_DIR, env=env, capture_output=True, text=True
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr[-3000:])
    return TEST_DB


@pytest.fixture
async def db(migrated_db: str):
    """Clean database state per test (TRUNCATE all tables except alembic_version)."""
    import sqlalchemy as sa

    from scout.config import get_settings
    from scout.db.engine import dispose_engine, get_engine
    from scout.util.pools import reset_pools

    get_settings.cache_clear()
    await dispose_engine()
    reset_pools()
    engine = get_engine()
    async with engine.begin() as conn:
        tables = (
            await conn.execute(
                sa.text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'"
                )
            )
        ).scalars().all()
        if tables:
            await conn.execute(sa.text("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE"))
    yield engine
    await dispose_engine()


@pytest.fixture
async def workspace(db):
    """A workspace + owner user. Returns (workspace_id, user_id)."""
    from scout.db.engine import session_scope
    from scout.db.models import User, Workspace, WorkspaceMember

    ws_id = uuid.uuid4()
    user_id = "user_" + uuid.uuid4().hex[:12]
    async with session_scope() as s:
        s.add(User(id=user_id, name="Test User", email=f"{user_id}@example.com"))
        s.add(Workspace(id=ws_id, name="Test", slug="test-" + uuid.uuid4().hex[:6]))
        await s.flush()
        s.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role="owner"))
    return ws_id, user_id
