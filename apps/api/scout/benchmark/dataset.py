"""Ground-truth dataset import: CSV (documented column mapping) or JSON items → validated benchmark items.

CSV columns (headers are case-insensitive; one row per expected person, rows of the same company merge
into one item; unknown columns are ignored with a warning):

=====================  ==========================================================================
company_domain         Expected company domain / website (also given to the engine, see company_input)
company_name           Expected company name (input when ``company_input = "name"``)
company_city           Input hint (website resolution when only the name is given)
company_country        Input hint (ISO-2)
company_catch_all      Expected catch-all truth for the domain (true / false)
company_email_pattern  Expected domain pattern (``{first}.{last}``, ``first.last``, ``prenom.nom``…)
person_first           Expected person first name
person_last            Expected person last name
person_name            Expected full name (alternative to first / last)
person_title           Expected title (role precision)
email                  Expected address of that person (or a company address when no person is set)
email_status           Expected status: SAFE / LIKELY_SAFE / CATCH_ALL / INVALID … (default: exists)
enrich:<column>        Expected value of an enrichment column (slug or name), e.g. ``enrich:uses_hubspot``
=====================  ==========================================================================

``email_status = INVALID`` marks an address (or, without address, a person) known to have no mailbox:
it feeds the invalid false-positive rate. Pure function module — no database access.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scout.benchmark.metrics import (
    name_tokens,
    names_match,
    norm_company,
    norm_domain,
    norm_email,
    norm_pattern,
    norm_status,
)
from scout.util.text import normalize_key

MAX_CSV_BYTES = 2_000_000
MAX_ROWS = 10_000
MAX_ITEMS = 2_000
MAX_PEOPLE_PER_ITEM = 50
MAX_EMAILS_PER_ITEM = 100
MAX_ENRICH_KEYS = 25
MAX_STR = 300
MAX_ERRORS = 50

Kind = Literal["leads", "email", "enrichment"]
CompanyInputMode = Literal["domain", "name"]

CSV_COLUMNS: dict[str, str] = {
    "company_domain": "Expected company domain / website — also the engine input unless company_input=name",
    "company_name": "Expected company name — the engine input when company_input=name",
    "company_city": "Input hint for website resolution",
    "company_country": "Input hint (ISO-2 country code)",
    "company_catch_all": "Expected catch-all truth for the domain (true/false)",
    "company_email_pattern": "Expected email pattern, e.g. {first}.{last} or prenom.nom",
    "person_first": "Expected person first name",
    "person_last": "Expected person last name",
    "person_name": "Expected person full name (instead of first/last)",
    "person_title": "Expected job title (role precision)",
    "email": "Expected email address of the person (company address when no person)",
    "email_status": "Expected status: SAFE, LIKELY_SAFE, CATCH_ALL, RISKY, INVALID (INVALID = no mailbox)",
    "enrich:<column>": "Expected value of an enrichment column (slug or name)",
}

_SYNONYMS: dict[str, tuple[str, ...]] = {
    "company_domain": ("domain", "website", "company_website", "site", "url", "company_url", "domaine"),
    "company_name": ("company", "entreprise", "societe", "organization", "organisation", "raison_sociale"),
    "company_city": ("city", "ville"),
    "company_country": ("country", "pays"),
    "company_catch_all": ("catch_all", "catchall"),
    "company_email_pattern": ("email_pattern", "pattern"),
    "person_first": ("first_name", "firstname", "first", "prenom"),
    "person_last": ("last_name", "lastname", "last", "nom", "surname"),
    "person_name": ("full_name", "name", "person", "nom_complet"),
    "person_title": ("title", "job_title", "role", "poste", "fonction"),
    "email": ("email_address", "mail", "e_mail", "courriel"),
    "email_status": ("status", "statut", "email_verdict"),
}

TEMPLATE_CSV = (
    "company_domain,company_name,person_first,person_last,person_title,email,email_status,enrich:uses_hubspot\n"
    "acme-demo.fr,Acme Demo,Marie,Dupont,Fondatrice,marie.dupont@acme-demo.fr,SAFE,true\n"
    "acme-demo.fr,Acme Demo,Jean,Martin,Directeur commercial,jean.martin@acme-demo.fr,SAFE,\n"
    "acme-demo.fr,Acme Demo,Paul,Leroy,Comptable,,INVALID,\n"
    "globex-demo.com,Globex Demo,John,Smith,CEO,john@globex-demo.com,LIKELY_SAFE,false\n"
)


class DatasetError(Exception):
    """Validation failure with row-level details (surfaced as a 422 by the API)."""

    def __init__(self, message: str, errors: list[dict[str, Any]]):
        super().__init__(message)
        self.errors = errors[:MAX_ERRORS]


@dataclass
class ParsedDataset:
    items: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)  # CSV header → canonical column
    rows: int = 0


# =============================================================================================
# JSON item schema (also the API body)
# =============================================================================================


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, str_max_length=MAX_STR)


class CompanyTruth(_Strict):
    domain: str | None = None
    name: str | None = None
    catch_all: bool | None = None
    email_pattern: str | None = None


class PersonTruth(_Strict):
    first: str | None = None
    last: str | None = None
    full_name: str | None = None
    title: str | None = None


class EmailTruth(_Strict):
    address: str | None = None
    status: str | None = None
    first: str | None = None
    last: str | None = None


class ExpectedIn(_Strict):
    company: CompanyTruth | None = None
    people: list[PersonTruth] | None = Field(default=None, max_length=MAX_PEOPLE_PER_ITEM)
    people_exhaustive: bool = True
    emails: list[EmailTruth] | None = Field(default=None, max_length=MAX_EMAILS_PER_ITEM)
    enrichment: dict[str, Any] | None = None


class CompanyInput(_Strict):
    domain: str | None = None
    name: str | None = None
    city: str | None = None
    country: str | None = None


class ItemInput(_Strict):
    company: CompanyInput | None = None
    people: list[PersonTruth] | None = Field(default=None, max_length=MAX_PEOPLE_PER_ITEM)


class ItemIn(_Strict):
    label: str | None = None
    input: ItemInput | None = None
    expected: ExpectedIn


class ColumnPrompt(_Strict):
    instruction: str | None = Field(default=None, max_length=1000)
    data_type: Literal["boolean", "text", "number", "url", "email", "enum", "json", "date"] | None = None


# =============================================================================================
# Normalization shared by CSV and JSON imports
# =============================================================================================


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:MAX_STR] or None


def _bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    s = normalize_key(str(v or ""))
    if s in ("true", "yes", "y", "oui", "1", "vrai"):
        return True
    if s in ("false", "no", "n", "non", "0", "faux"):
        return False
    return None


def _person_input(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"first": p.get("first"), "last": p.get("last"), "full_name": p.get("full_name")} for p in people]


def finalize_item(
    raw: dict[str, Any],
    *,
    kind: Kind,
    company_input: CompanyInputMode = "domain",
    where: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate + normalize one item ``{label?, input?, expected}`` → (item, errors)."""
    errors: list[dict[str, Any]] = []
    try:
        item = ItemIn.model_validate(raw)
    except ValidationError as exc:
        for e in exc.errors()[:5]:
            errors.append({"where": where, "field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]})
        return {}, errors
    exp = item.expected
    company = exp.company.model_dump(exclude_none=True) if exp.company else {}
    if company.get("domain"):
        dom = norm_domain(company["domain"])
        if dom is None:
            errors.append(
                {
                    "where": where,
                    "field": "company.domain",
                    "message": f"Not a public domain: {company['domain']!r}",
                }
            )
        else:
            company["domain"] = dom
    if company.get("email_pattern"):
        pat = norm_pattern(company["email_pattern"])
        if pat is None:
            errors.append(
                {"where": where, "field": "company.email_pattern", "message": "Unrecognized pattern"}
            )
        company["email_pattern"] = pat
    people = [p.model_dump(exclude_none=True) for p in exp.people] if exp.people is not None else None
    for p in people or []:
        if not name_tokens(p.get("first"), p.get("last"), p.get("full_name")):
            errors.append(
                {"where": where, "field": "people", "message": "A person needs a first, last or full name"}
            )
    emails: list[dict[str, Any]] | None = None
    if exp.emails is not None:
        emails = []
        for et in exp.emails:
            d = et.model_dump(exclude_none=True)
            if d.get("address"):
                addr = norm_email(d["address"])
                if addr is None:
                    errors.append(
                        {
                            "where": where,
                            "field": "emails.address",
                            "message": f"Invalid address: {d['address']!r}",
                        }
                    )
                    continue
                d["address"] = addr
            if d.get("status"):
                st = norm_status(d["status"])
                if st is None:
                    errors.append(
                        {
                            "where": where,
                            "field": "emails.status",
                            "message": f"Unknown status: {d['status']!r}",
                        }
                    )
                    continue
                d["status"] = st
            if not d.get("address") and d.get("status") != "INVALID":
                continue  # nothing to compare
            emails.append(d)
    enrichment: dict[str, Any] | None = None
    if exp.enrichment:
        if len(exp.enrichment) > MAX_ENRICH_KEYS:
            errors.append(
                {"where": where, "field": "enrichment", "message": f"At most {MAX_ENRICH_KEYS} columns"}
            )
        values: dict[str, Any] = {}
        for k, v in list(exp.enrichment.items())[:MAX_ENRICH_KEYS]:
            key = str(k).strip()[:80]
            if not key:
                continue
            if isinstance(v, dict) or (isinstance(v, list) and any(isinstance(x, dict | list) for x in v)):
                errors.append(
                    {
                        "where": where,
                        "field": f"enrichment.{key}",
                        "message": "Values must be scalars or lists of scalars",
                    }
                )
                continue
            if isinstance(v, str):
                v = v.strip()[:MAX_STR]
            if v is not None and v != "":
                values[key] = v
        enrichment = values or None
    if not (company or people or emails or enrichment):
        errors.append({"where": where, "field": "expected", "message": "Nothing expected for this item"})
    # input: explicit, else derived from the truth (identity handed to the engine)
    inp: dict[str, Any] = {}
    if item.input and item.input.company:
        ci = item.input.company.model_dump(exclude_none=True)
        if ci.get("domain"):
            ci["domain"] = norm_domain(ci["domain"]) or ci["domain"]
        inp["company"] = ci
    elif company:
        if company_input == "name":
            if not company.get("name"):
                errors.append(
                    {
                        "where": where,
                        "field": "company.name",
                        "message": "company_input=name needs a company name",
                    }
                )
            inp["company"] = {"name": company.get("name")}
        else:
            inp["company"] = {k: company[k] for k in ("domain", "name") if company.get(k)}
    if item.input and item.input.people:
        inp["people"] = [p.model_dump(exclude_none=True) for p in item.input.people]
    elif kind == "email" and people:
        inp["people"] = _person_input(people)
    if not inp.get("company") or not (inp["company"].get("domain") or inp["company"].get("name")):
        errors.append(
            {"where": where, "field": "input.company", "message": "A company domain or name is required"}
        )
    expected: dict[str, Any] = {"company": company or None, "people_exhaustive": exp.people_exhaustive}
    if people is not None:
        expected["people"] = people
    if emails:
        expected["emails"] = emails
    if enrichment:
        expected["enrichment"] = enrichment
    label = item.label or (
        company.get("domain") or company.get("name") or (inp.get("company") or {}).get("name")
    )
    return {"label": label, "input": inp, "expected": expected}, errors


def validate_items(
    items: list[dict[str, Any]], *, kind: Kind, company_input: CompanyInputMode = "domain"
) -> list[dict[str, Any]]:
    if not items:
        raise DatasetError("The dataset has no items", [])
    if len(items) > MAX_ITEMS:
        raise DatasetError(f"Too many items ({len(items)}); the limit is {MAX_ITEMS}", [])
    out: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for i, raw in enumerate(items):
        item, errs = finalize_item(raw, kind=kind, company_input=company_input, where=f"item {i + 1}")
        errors.extend(errs)
        if not errs:
            out.append(item)
    if errors:
        raise DatasetError(f"{len(errors)} validation error(s)", errors)
    return out


# =============================================================================================
# CSV
# =============================================================================================


def map_headers(headers: list[str]) -> tuple[dict[str, str], list[str]]:
    """CSV header → canonical column (or ``enrich:<key>``); returns (mapping, ignored headers)."""
    mapping: dict[str, str] = {}
    ignored: list[str] = []
    used: set[str] = set()
    for h in headers:
        raw = (h or "").strip()
        if raw.lower().startswith(("enrich:", "enrich.")):
            key = raw[7:].strip()
            if key:
                mapping[h] = f"enrich:{key}"
                continue
        k = normalize_key(raw).replace(" ", "_")
        target = k if k in _SYNONYMS else next((t for t, syn in _SYNONYMS.items() if k in syn), None)
        if target and target not in used:
            mapping[h] = target
            used.add(target)
        else:
            ignored.append(raw)
    return mapping, ignored


def _read_csv(text: str) -> tuple[list[str], list[list[str]]]:
    if len(text.encode("utf-8", "ignore")) > MAX_CSV_BYTES:
        raise DatasetError(f"CSV too large (max {MAX_CSV_BYTES // 1_000_000} MB)", [])
    text = text.lstrip("﻿")
    try:
        dialect: Any = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        raise DatasetError("The CSV is empty", [])
    if len(rows) - 1 > MAX_ROWS:
        raise DatasetError(f"Too many rows ({len(rows) - 1}); the limit is {MAX_ROWS}", [])
    return [h.strip() for h in rows[0]], rows[1:]


def parse_csv(
    text: str,
    *,
    kind: Kind = "leads",
    people_exhaustive: bool = True,
    company_input: CompanyInputMode = "domain",
) -> ParsedDataset:
    """CSV text → validated items (one per company). Raises DatasetError with row numbers."""
    headers, body = _read_csv(text)
    mapping, ignored = map_headers(headers)
    targets = set(mapping.values())
    warnings = [f"Ignored column: {h}" for h in ignored if h]
    if not targets & {"company_domain", "company_name"}:
        raise DatasetError("Map a company_domain or company_name column", [])
    has_people = bool(targets & {"person_first", "person_last", "person_name"})
    has_emails = "email" in targets or "email_status" in targets
    idx = {t: headers.index(h) for h, t in mapping.items()}
    errors: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def cell(row: list[str], target: str) -> str | None:
        i = idx.get(target)
        return _clean(row[i]) if i is not None and i < len(row) else None

    for n, row in enumerate(body, start=2):
        where = f"row {n}"
        domain_raw, name = cell(row, "company_domain"), cell(row, "company_name")
        domain = norm_domain(domain_raw) if domain_raw else None
        if domain_raw and domain is None:
            errors.append(
                {"where": where, "field": "company_domain", "message": f"Not a public domain: {domain_raw!r}"}
            )
            continue
        if not domain and not name:
            errors.append(
                {"where": where, "field": "company", "message": "company_domain or company_name is required"}
            )
            continue
        key = f"d:{domain}" if domain else f"n:{norm_company(name)}"
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "company": {},
                "input": {},
                "people": [],
                "emails": [],
                "enrichment": {},
            }
            order.append(key)
        comp = g["company"]
        for target, field_name in (("company_name", "name"), ("company_email_pattern", "email_pattern")):
            v = cell(row, target)
            if v and not comp.get(field_name):
                comp[field_name] = v
        if domain:
            comp["domain"] = domain
        ca = cell(row, "company_catch_all")
        if ca is not None:
            b = _bool(ca)
            if b is None:
                errors.append(
                    {
                        "where": where,
                        "field": "company_catch_all",
                        "message": f"Expected true/false, got {ca!r}",
                    }
                )
            elif comp.get("catch_all") is not None and comp["catch_all"] != b:
                errors.append(
                    {
                        "where": where,
                        "field": "company_catch_all",
                        "message": "Conflicting catch-all values for this company",
                    }
                )
            else:
                comp["catch_all"] = b
        for target, field_name in (("company_city", "city"), ("company_country", "country")):
            v = cell(row, target)
            if v and not g["input"].get(field_name):
                g["input"][field_name] = v
        first, last, full = cell(row, "person_first"), cell(row, "person_last"), cell(row, "person_name")
        title = cell(row, "person_title")
        if full and not (first or last):
            parts = full.split()
            first, last = (parts[0], " ".join(parts[1:]) or None) if len(parts) > 1 else (None, parts[0])
        toks = name_tokens(first, last)
        if toks:
            existing = next(
                (p for p in g["people"] if names_match(name_tokens(p.get("first"), p.get("last")), toks)),
                None,
            )
            if existing is None:
                if len(g["people"]) >= MAX_PEOPLE_PER_ITEM:
                    errors.append(
                        {
                            "where": where,
                            "field": "person",
                            "message": f"More than {MAX_PEOPLE_PER_ITEM} people for one company",
                        }
                    )
                    continue
                g["people"].append(
                    {k: v for k, v in (("first", first), ("last", last), ("title", title)) if v}
                )
            elif title and not existing.get("title"):
                existing["title"] = title
        email, status = cell(row, "email"), cell(row, "email_status")
        if email or status:
            addr = norm_email(email) if email else None
            if email and addr is None:
                errors.append({"where": where, "field": "email", "message": f"Invalid address: {email!r}"})
                continue
            st = norm_status(status) if status else None
            if status and st is None:
                errors.append(
                    {"where": where, "field": "email_status", "message": f"Unknown status: {status!r}"}
                )
                continue
            if addr or (st == "INVALID" and toks):
                entry = {
                    k: v
                    for k, v in (("address", addr), ("status", st), ("first", first), ("last", last))
                    if v
                }
                if len(g["emails"]) < MAX_EMAILS_PER_ITEM:
                    g["emails"].append(entry)
        for target, col in mapping.items():
            if not col.startswith("enrich:"):
                continue
            v = cell(row, col)
            if v is None:
                continue
            k = col[7:]
            prev = g["enrichment"].get(k)
            if prev is not None and normalize_key(prev) != normalize_key(v):
                errors.append(
                    {
                        "where": where,
                        "field": target,
                        "message": f"Conflicting values for {k!r} in this company",
                    }
                )
            else:
                g["enrichment"][k] = v
        if len(errors) >= MAX_ERRORS:
            break
    if len(groups) > MAX_ITEMS:
        errors.append(
            {
                "where": "file",
                "field": "company",
                "message": f"Too many companies ({len(groups)}); the limit is {MAX_ITEMS}",
            }
        )
    if errors:
        raise DatasetError(f"{len(errors)} invalid row(s)", errors)
    items: list[dict[str, Any]] = []
    for i, key in enumerate(order):
        g = groups[key]
        raw: dict[str, Any] = {
            "expected": {
                "company": g["company"] or None,
                "people_exhaustive": people_exhaustive,
                **({"people": g["people"]} if has_people else {}),
                **({"emails": g["emails"]} if has_emails and g["emails"] else {}),
                **({"enrichment": g["enrichment"]} if g["enrichment"] else {}),
            }
        }
        if g["input"]:
            comp_in: dict[str, Any] = {k: v for k, v in g["company"].items() if k in ("domain", "name")}
            if company_input == "name":
                comp_in.pop("domain", None)
            raw["input"] = {"company": {**comp_in, **g["input"]}}
            if kind == "email" and g["people"]:
                raw["input"]["people"] = _person_input(g["people"])
        item, errs = finalize_item(raw, kind=kind, company_input=company_input, where=f"company {i + 1}")
        if errs:
            errors.extend(errs)
        else:
            items.append(item)
    if errors:
        raise DatasetError(f"{len(errors)} invalid item(s)", errors)
    if not items:
        raise DatasetError("The CSV has no data rows", [])
    return ParsedDataset(
        items=items, warnings=warnings, mapping={h: t for h, t in mapping.items()}, rows=len(body)
    )


def summarize(items: list[dict[str, Any]]) -> dict[str, int]:
    """Counts shown in the import preview."""
    s = {"items": len(items), "people": 0, "emails": 0, "invalid_emails": 0, "enrichment_values": 0}
    for it in items:
        exp = it.get("expected") or {}
        s["people"] += len(exp.get("people") or [])
        for e in exp.get("emails") or []:
            s["emails" if e.get("status") != "INVALID" else "invalid_emails"] += 1
        s["enrichment_values"] += len(exp.get("enrichment") or {})
    return s
