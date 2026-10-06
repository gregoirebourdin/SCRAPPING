"""French communes from the official geo API (geo.api.gouv.fr): INSEE code, postal codes, department.

City-precise discovery and qualification: "agences web à Annecy" means the commune of Annecy (INSEE 74010,
postal codes 74000 / 74370 / 74600 / 74940 / 74960 — its merged former communes included), not Thonon-les-Bains
in the same department. Resolution is async (``resolve`` / ``prefetch``) and cached per process; the synchronous
readers (source plans, the location gate) use the cache and fall back to department-level matching when a name
could not be resolved — never to a guess.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import structlog

from scout.config import get_settings
from scout.util.text import normalize_key

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Commune:
    code: str  # INSEE code, e.g. "74010"
    name: str
    department: str
    postal_codes: tuple[str, ...]
    population: int = 0


_cache: dict[str, Commune | None] = {}


def _key(name: str) -> str:
    return normalize_key(name or "").replace("-", " ").strip()


# Paris, Lyon and Marseille: establishments are registered under their arrondissements' INSEE codes, never
# under the city's own code (75056 / 69123 / 13055), so the registry must be queried with those.
_ARRONDISSEMENTS: dict[str, tuple[str, ...]] = {
    "75056": tuple(f"751{n:02d}" for n in range(1, 21)),
    "69123": tuple(f"6938{n}" for n in range(1, 10)),
    "13055": tuple(f"132{n:02d}" for n in range(1, 17)),
}


def registry_codes(c: Commune) -> tuple[str, ...]:
    """INSEE codes to query the company registry with for this commune."""
    return _ARRONDISSEMENTS.get(c.code, (c.code,))


def remember(c: Commune, *aliases: str) -> None:
    """Seed the cache (tests, or a commune resolved elsewhere)."""
    for n in (c.name, *aliases):
        _cache[_key(n)] = c


def clear_cache() -> None:
    _cache.clear()


def cached(name: str) -> Commune | None:
    return _cache.get(_key(name))


def pick(name: str, rows: Iterable[dict]) -> Commune | None:
    """The commune named exactly ``name`` (accents / case / hyphens ignored), most populous first."""
    want = _key(name)
    best: Commune | None = None
    for r in rows:
        if _key(str(r.get("nom") or "")) != want:
            continue
        c = Commune(
            code=str(r.get("code") or ""),
            name=str(r.get("nom") or name),
            department=str(r.get("codeDepartement") or ""),
            postal_codes=tuple(str(p) for p in (r.get("codesPostaux") or [])),
            population=int(r.get("population") or 0),
        )
        if c.code and (best is None or c.population > best.population):
            best = c
    return best


async def resolve(name: str) -> Commune | None:
    """Commune for a French city name, or None (unknown name, API unreachable). Cached per process."""
    key = _key(name)
    if not key:
        return None
    if key in _cache:
        return _cache[key]
    base = get_settings().geo_api_url
    if not base:
        return None
    from scout.discovery.common import http_request
    from scout.errors import JobError

    try:
        resp = await http_request(
            "GET",
            base.rstrip("/") + "/communes",
            source="geo_api",
            params={
                "nom": name,
                "fields": "code,nom,codesPostaux,population,codeDepartement",
                "boost": "population",
                "limit": 10,
            },
            timeout_s=8.0,
        )
        rows = resp.json()
    except (JobError, ValueError) as exc:  # transient: do not cache, the department fallback applies
        log.info("communes.resolve_failed", city=name, error=str(exc)[:200])
        return None
    c = pick(name, rows if isinstance(rows, list) else [])
    _cache[key] = c
    return c


async def prefetch(cities: Iterable[str]) -> None:
    for city in cities:
        if city and city.strip():
            await resolve(city)


def postal_codes_for(cities: Iterable[str]) -> set[str] | None:
    """All postal codes of the requested communes, or None when any of them is not resolved (yet)."""
    out: set[str] = set()
    for city in cities:
        c = cached(city)
        if c is None:
            return None
        out.update(c.postal_codes)
    return out or None


def commune_names(cities: Iterable[str]) -> set[str]:
    return {_key(c.name) for city in cities if (c := cached(city)) is not None} | {_key(x) for x in cities}


def same_place(city: str | None, cities: Iterable[str]) -> bool:
    return bool(city) and _key(city or "") in commune_names(cities)
