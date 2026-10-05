"""Async engine/session management (asyncpg). Neon-compatible (pooled URL for runtime)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import orjson
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from scout.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _json_serializer(obj: Any) -> str:
    return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_UUID).decode()


def normalize_async_url(url: str) -> str:
    """Accept postgres:// / postgresql:// URLs (Neon, Railway) and force the asyncpg driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    # asyncpg uses `ssl=` instead of libpq's `sslmode=`; drop libpq-only params.
    if "?" in url:
        base, query = url.split("?", 1)
        params = []
        for part in query.split("&"):
            key = part.split("=", 1)[0]
            if key == "sslmode":
                value = part.split("=", 1)[1] if "=" in part else "require"
                params.append(f"ssl={value}")
            elif key in {"channel_binding", "options"}:
                continue
            else:
                params.append(part)
        url = base + ("?" + "&".join(params) if params else "")
    return url


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(
            normalize_async_url(s.database_url),
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_pre_ping=True,
            pool_recycle=300,
            json_serializer=_json_serializer,
            json_deserializer=orjson.loads,
            # Neon's pooler (PgBouncer, transaction mode) — avoid server-side prepared statement reuse.
            connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, rollback on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
