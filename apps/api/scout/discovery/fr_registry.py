"""French company registry (recherche-entreprises.api.gouv.fr: SIRENE + RNE). Free, no key, ~7 req/s.

Query plan = NAF code group × department (economic order), with the INSEE headcount filter derived from the
ICP's employee range. Plans are cumulative: ``plan(expansion=n)`` ⊇ ``plan(expansion=n-1)`` with stable keys.
"""

from __future__ import annotations

import re
import time
from typing import Any

import structlog

from scout.config import get_settings
from scout.db.enums import UsageCategory
from scout.discovery import geo
from scout.discovery.base import DiscoveryPage, DiscoveryQuery, RawCandidate
from scout.discovery.common import (
    Throttle,
    employee_bounds,
    http_request,
    is_french_context,
    is_local_icp,
    profiles_for,
)
from scout.discovery.employee_bands import range_for_tranche, tranches_for_range
from scout.discovery.naf import naf_category
from scout.errors import FetchError
from scout.schemas.campaign import CampaignDefinition
from scout.services.usage import record_usage
from scout.util.pools import pool
from scout.util.text import collapse_ws, title_case_name

log = structlog.get_logger(__name__)

PER_PAGE = 25
MAX_PAGE = 400            # the API serves at most 10,000 results per query
TOP_DEPARTMENTS = 20      # no geographic restriction: biggest departments first, all of them on expansion
ANNUAIRE_URL = "https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}"

_PAREN_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")
_EXCLUDED_ROLES = re.compile(r"commissaire aux comptes", re.I)

_throttle = Throttle(1 / 6)  # ≤ 6 req/s (documented limit ~7 req/s)


def _strip_sigle(name: str) -> str:
    """'ACME CONSEIL (ACME)' → 'ACME CONSEIL'."""
    out = _PAREN_SUFFIX.sub("", name or "").strip()
    return out or (name or "").strip()


def _clean_trade_name(value: str | None) -> str | None:
    """A single, usable trade name (lists such as 'A; B; C' or 'A - B - C' are ambiguous → None)."""
    if not value:
        return None
    v = collapse_ws(value)
    if not v or ";" in v or " - " in v or len(v) > 50:
        return None
    return v


def _display_name(item: dict[str, Any]) -> str:
    siege = item.get("siege") or {}
    candidates = [siege.get("nom_commercial"), *((siege.get("liste_enseignes") or [])[:1])]
    for c in candidates:
        clean = _clean_trade_name(c)
        if clean:
            return clean
    return _strip_sigle(item.get("nom_complet") or item.get("nom_raison_sociale") or "")


def _to_float(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _people(item: dict[str, Any], source_url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in item.get("dirigeants") or []:
        if d.get("type_dirigeant") != "personne physique":
            continue
        qualite = (d.get("qualite") or "").strip()
        if _EXCLUDED_ROLES.search(qualite):  # statutory auditors are external, not company people
            continue
        prenoms = collapse_ws(d.get("prenoms") or "")
        nom = _strip_sigle(collapse_ws(d.get("nom") or ""))  # 'GUILLOT (DUPESSEY)' → 'GUILLOT'
        if not nom:
            continue
        first = prenoms.split(" ")[0] if prenoms else ""
        full = title_case_name(f"{first} {nom}".strip())
        out.append({
            "full_name": full,
            "first_name": title_case_name(first) if first else None,
            "last_name": title_case_name(nom),
            "title": qualite or None,
            "source_url": source_url,
            "raw": {"prenoms": prenoms, "nom": d.get("nom"), "qualite": qualite},
        })
    return out


def map_result(item: dict[str, Any], *, departments: list[str] | None = None) -> RawCandidate | None:
    """One API result → RawCandidate (website left to the pipeline's resolver)."""
    siren = (item.get("siren") or "").strip()
    if not siren:
        return None
    siege = item.get("siege") or {}
    source_url = ANNUAIRE_URL.format(siren=siren)
    tranche = item.get("tranche_effectif_salarie") or siege.get("tranche_effectif_salarie")
    emp_min, emp_max = range_for_tranche(tranche)
    naf = item.get("activite_principale") or siege.get("activite_principale")
    region_code = siege.get("region")
    region = geo.FR_REGIONS.get(region_code) if region_code else None
    location = {
        "country": "FR",
        "address": siege.get("adresse"),
        "postal_code": siege.get("code_postal"),
        "city": siege.get("libelle_commune"),
        "department": siege.get("departement"),
        "region": region.name if region else region_code,
        "lat": _to_float(siege.get("latitude")),
        "lng": _to_float(siege.get("longitude")),
    }
    etat = item.get("etat_administratif") or siege.get("etat_administratif")
    matching = [
        {k: e.get(k) for k in ("siret", "adresse", "code_postal", "libelle_commune", "est_siege", "etat_administratif")}
        for e in (item.get("matching_etablissements") or [])[:5]
    ]
    raw = {
        "siren": siren,
        "siret_siege": siege.get("siret"),
        "nom_complet": item.get("nom_complet"),
        "nom_raison_sociale": item.get("nom_raison_sociale"),
        "sigle": item.get("sigle"),
        "nom_commercial": siege.get("nom_commercial"),
        "enseignes": siege.get("liste_enseignes"),
        "naf": naf,
        "naf25": item.get("activite_principale_naf25"),
        "tranche_effectif_salarie": tranche,
        "annee_tranche_effectif_salarie": item.get("annee_tranche_effectif_salarie"),
        "categorie_entreprise": item.get("categorie_entreprise"),
        "nature_juridique": item.get("nature_juridique"),
        "date_creation": item.get("date_creation"),
        "nombre_etablissements_ouverts": item.get("nombre_etablissements_ouverts"),
        "matching_etablissements": matching,
    }
    if departments:
        raw["siege_in_query_area"] = siege.get("departement") in departments
    return RawCandidate(
        source="fr_registry",
        source_entity_id=siren,
        name=_display_name(item),
        website=None,
        location={k: v for k, v in location.items() if v not in (None, "")},
        category=naf_category(naf),
        source_url=source_url,
        raw_data=raw,
        employee_min=emp_min,
        employee_max=emp_max,
        registry_source="fr_sirene",
        registry_id=siren,
        status="active" if etat == "A" else ("closed" if etat == "C" else None),
        people=_people(item, source_url),
    )


class FrRegistrySource:
    key = "fr_registry"
    name = "French company registry (SIRENE / RNE)"
    quality = 0.95
    cost_class = "FREE"

    def __init__(self, *, throttle: Throttle | None = None) -> None:
        self.throttle = throttle or _throttle

    def is_configured(self) -> bool:
        return bool(get_settings().fr_registry_url)

    def suitability(self, defn: CampaignDefinition) -> float:
        if not is_french_context(defn):
            return 0.0
        profiles = profiles_for(defn)
        if not any(p.naf_codes for p in profiles):
            return 0.0  # without an activity filter the registry is just noise
        # Exhaustive and official for every French company; no websites, so slightly behind Maps for storefronts.
        base = 0.75 if is_local_icp(profiles) else 1.0
        return base if defn.company_filters.countries else base * 0.8

    def _naf_codes(self, defn: CampaignDefinition, *, adjacent: bool) -> list[str]:
        codes: list[str] = []
        for p in profiles_for(defn)[:3]:
            codes.extend(p.all_naf(adjacent=adjacent))
        return list(dict.fromkeys(codes))

    def plan(self, defn: CampaignDefinition, *, expansion: int = 0) -> list[DiscoveryQuery]:
        cf = defn.company_filters
        primary = self._naf_codes(defn, adjacent=False)
        if not primary:
            return []
        adjacent = [c for c in self._naf_codes(defn, adjacent=True) if c not in primary]
        restricted = geo.departments_for(cf.regions, cf.cities)
        if restricted:
            departments = restricted
        else:
            all_deps = list(geo.DEPARTMENTS_BY_ECONOMIC_SIZE)
            departments = all_deps if expansion >= 1 else all_deps[:TOP_DEPARTMENTS]
        lo, hi = employee_bounds(defn)
        tranches = tranches_for_range(lo, hi)
        groups = [primary] + ([adjacent] if expansion >= 1 and adjacent else [])
        queries: list[DiscoveryQuery] = []
        for gi, naf in enumerate(groups):
            for rank, dep in enumerate(departments):
                queries.append(
                    DiscoveryQuery(
                        key=f"naf:{','.join(naf)}|dep:{dep}" + (f"|t:{','.join(tranches)}" if tranches else ""),
                        params={"naf": naf, "departement": dep, "tranches": tranches, "restricted": bool(restricted)},
                        weight=round((1.0 if gi == 0 else 0.6) / (1 + rank * 0.05), 4),
                    )
                )
        return queries

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage:
        page = int((cursor or {}).get("page", 1))
        params: dict[str, Any] = {
            "activite_principale": ",".join(query.params["naf"]),
            "departement": query.params["departement"],
            "etat_administratif": "A",
            "per_page": PER_PAGE,
            "page": page,
            "minimal": "true",
            "include": "siege,dirigeants,matching_etablissements",
        }
        if query.params.get("tranches"):
            params["tranche_effectif_salarie"] = ",".join(query.params["tranches"])
        url = get_settings().fr_registry_url.rstrip("/") + "/search"
        started = time.monotonic()
        async with pool("public_api"):
            await self.throttle.wait()
            resp = await http_request("GET", url, source=self.key, params=params, timeout_s=20.0)
        await record_usage(UsageCategory.registry_request, source_key=self.key, resolver="discovery")
        try:
            body = resp.json()
        except ValueError as exc:
            raise FetchError(f"{self.key}: invalid JSON response") from exc
        deps = [query.params["departement"]] if query.params.get("restricted") else None
        candidates = [c for c in (map_result(r, departments=deps) for r in body.get("results") or []) if c]
        total_pages = int(body.get("total_pages") or 0)
        exhausted = page >= min(total_pages, MAX_PAGE) or not candidates
        log.debug(
            "fr_registry.page", query=query.key, page=page, total_pages=total_pages, n=len(candidates),
            ms=int((time.monotonic() - started) * 1000),
        )
        return DiscoveryPage(candidates=candidates, next_cursor=None if exhausted else {"page": page + 1})
