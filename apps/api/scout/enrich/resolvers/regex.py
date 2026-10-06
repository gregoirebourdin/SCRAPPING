"""Regex strategy: deterministic pattern on cached text, with pattern/length caps and a scan timeout."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence

from scout.db.enums import ColumnDataType
from scout.enrich.matching import excerpt
from scout.enrich.resolvers.base import NOT_CRAWLED, ResolveContext, failed, ok, ordered_pages, unknown
from scout.enrich.types import CellResult

RESOLVER = "regex"
MAX_PATTERN_CHARS = 300
MAX_SCAN_CHARS = 60_000
SCAN_TIMEOUT_S = 2.0
# Nested quantifiers such as (a+)+ or (\w*)* are the classic catastrophic-backtracking shapes.
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*[+*{]")


class UnsafePattern(ValueError):
    pass


def compile_safe(pattern: str | None) -> re.Pattern[str]:
    """Compile a user/planner pattern case-insensitively, rejecting oversized or backtracking-prone ones."""
    if not pattern or len(pattern) > MAX_PATTERN_CHARS:
        raise UnsafePattern("Pattern is empty or too long")
    if _NESTED_QUANTIFIER.search(pattern):
        raise UnsafePattern("Pattern uses nested quantifiers")
    try:
        return re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        raise UnsafePattern(f"Invalid pattern: {exc}") from exc


def _scan(rx: re.Pattern[str], docs: Sequence[tuple[str, str]]) -> tuple[str, str, int, int, str] | None:
    for url, text in docs:
        m = rx.search(text[:MAX_SCAN_CHARS])
        if m:
            group = 1 if rx.groups and m.group(1) else 0
            return url, text, m.start(group), m.end(group), m.group(group)
    return None


async def find_pattern(rx: re.Pattern[str], pages: Sequence[object]) -> tuple[str, str, int, int, str] | None:
    """First match across pages (in a worker thread, bounded by SCAN_TIMEOUT_S)."""
    docs = [(getattr(p, "url", ""), getattr(p, "content_text", "") or "") for p in pages]
    return await asyncio.wait_for(asyncio.to_thread(_scan, rx, docs), timeout=SCAN_TIMEOUT_S)


def _normalize(value: str, field: str | None) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if field in {"siren", "siret"}:
        return re.sub(r"\D", "", value)
    if field == "vat":
        return re.sub(r"\s", "", value).upper()
    return value


async def resolve(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    try:
        rx = compile_safe(plan.pattern)
    except UnsafePattern as exc:
        return failed(plan, resolver=RESOLVER, error=str(exc))
    if not rc.pages:
        return unknown(plan, resolver=RESOLVER, error=NOT_CRAWLED)
    preferred = ordered_pages(rc.pages, plan.input_sources) if plan.input_sources else []
    rest = [p for p in ordered_pages(rc.pages) if p not in preferred]
    try:
        found = await find_pattern(rx, [*preferred, *rest])
    except TimeoutError:
        return failed(plan, resolver=RESOLVER, error="Pattern too slow to evaluate")
    boolean = plan.data_type == ColumnDataType.boolean
    if found:
        url, text, s, e, raw = found
        value = True if boolean else _normalize(raw, plan.field)
        return ok(plan, value, resolver=RESOLVER, confidence=0.95, evidence=excerpt(text, s, e, 160), source_url=url)
    evidence = f"No match across {len(rc.pages)} crawled pages"
    if boolean:
        return ok(plan, False, resolver=RESOLVER, confidence=0.85, evidence=evidence)
    return unknown(plan, resolver=RESOLVER, evidence=evidence, source_id="website")
