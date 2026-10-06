"""'Resume with changes' — turn a short instruction into a structured amendment and a reviewable diff.

* `parse_instruction` maps text such as "ajoute aussi Marseille", "seulement les fondateurs", "+200 leads",
  "budget 10 $" or "2-30 employees" onto a :class:`CampaignAmendment` with the deterministic ICP parser
  (`scout.pipeline.icp.heuristic_parse`) plus explicit add / replace / remove cues. Nothing the user did not
  express is changed.
* `apply_amendment` applies it to the campaign definition and returns the human diff used for confirmation.
"""

from __future__ import annotations

import re
from typing import Any

from unidecode import unidecode

from scout.db.enums import CampaignMode, EmailStatus
from scout.errors import ValidationFailed
from scout.schemas.campaign import (
    AmendmentChange,
    CampaignAmendment,
    CampaignDefinition,
    EmployeeRange,
    _country_name,
)

_ADD = re.compile(
    r"\b(aussi|also|ajoute\w*|add|added|plus|en plus|as well|too|et aussi|include|inclu\w*)\b", re.I
)
_ONLY = re.compile(
    r"\b(seulement|only|uniquement|just|juste|plut[oô]t|instead|[àa] la place|exclusivement)\b", re.I
)
_REMOVE = re.compile(
    r"\b(retire\w*|enl[eè]ve\w*|remove|sans|except|hors|pas|no more|plus de|exclu\w*|exclude|drop)\b", re.I
)
_NUM = r"(\d[\d  ,.]*\s*[kK]?)"
_LEADS = r"(?:leads?|contacts?|prospects?|personnes|people|entreprises|companies|agences?|r[ée]sultats?)"


def _to_int(raw: str) -> int:
    s = raw.strip().lower()
    mult = 1000 if s.endswith("k") else 1
    s = re.sub(r"[^\d]", "", s.rstrip("k"))
    return int(s or 0) * mult


def _money(text: str) -> tuple[float, bool] | None:
    """('budget 10$', '+5 €', 'augmente le budget de 5') → (amount, relative)."""
    low = text.lower()
    m = re.search(r"(\+)?\s*(\d+(?:[.,]\d+)?)\s*(\$|€|usd|eur|dollars?|euros?)", low) or re.search(
        r"(\$|€)\s*(\d+(?:[.,]\d+)?)", low
    )
    if m:
        if m.group(1) in ("$", "€"):
            amount = float(m.group(2).replace(",", "."))
            relative = bool(re.search(r"\+\s*[\$€]", low))
        else:
            amount = float(m.group(2).replace(",", "."))
            relative = bool(m.group(1))
    else:
        m2 = re.search(r"budget\D{0,20}?(\d+(?:[.,]\d+)?)", low)
        if not m2:
            return None
        amount = float(m2.group(1).replace(",", "."))
        relative = False
    if re.search(r"(raise|increase|augmente|rajoute|ajoute|add)\w*\s+(the\s+|le\s+)?budget\s+(by|de)\b", low):
        relative = True
    return amount, relative


def parse_instruction(instruction: str, base: CampaignDefinition) -> CampaignAmendment:
    """Deterministic NL → amendment. Raises ValidationFailed when nothing maps to the search criteria."""
    from scout.pipeline.icp import FR_CITIES, TITLE_WORDS, _industries_from_text, heuristic_parse

    text = (instruction or "").strip()
    if not text:
        return CampaignAmendment()
    text = re.sub(r"\b(cmo|cto|cso|coo|cfo)s\b", r"\1", text, flags=re.I)
    low = text.lower()
    folded = unidecode(low)
    am: dict[str, Any] = {"instruction": text}

    # ---- budget (before the lead count: "$10" is not ten leads) ----
    money = _money(text)
    if money is not None and re.search(r"budget|\$|€|usd|eur|dollar|euro", low):
        amount, relative = money
        cur = base.limits.max_cost_usd or 0.0
        am["max_cost_usd"] = round(cur + amount if relative else amount, 2)
        low_nomoney = re.sub(
            r"(\+)?\s*\d+(?:[.,]\d+)?\s*(\$|€|usd|eur|dollars?|euros?)|(\$|€)\s*\d+(?:[.,]\d+)?", " ", low
        )
        low_nomoney = re.sub(r"budget\D{0,20}?\d+(?:[.,]\d+)?", " ", low_nomoney)
    else:
        low_nomoney = low

    # ---- target ----
    rel = re.search(rf"\+\s*{_NUM}", low_nomoney) or re.search(
        rf"{_NUM}\s*(?:{_LEADS}\s*)?(?:de plus|more|suppl[ée]mentaires?|additional|en plus|autres|extra)\b",
        low_nomoney,
    )
    if not rel:
        rel = re.search(rf"\b(?:ajoute\w*|add|rajoute\w*|encore)\s+{_NUM}\s*{_LEADS}", low_nomoney)
    absolute = re.search(
        rf"(?:target|objectif|cible|jusqu.?[àa]|up to|go to|monte\w*\s+[àa]|raise to|passe\w*\s+[àa])\s*(?:de\s+)?{_NUM}",
        low_nomoney,
    ) or re.search(rf"{_NUM}\s*{_LEADS}\s*(?:au total|in total|total)", low_nomoney)
    if rel:
        n = _to_int(rel.group(1))
        if n:
            am["add_target"] = min(100_000, n)
    elif absolute:
        n = _to_int(absolute.group(1))
        if n:
            am["target_qualified_count"] = min(100_000, n)

    # ---- geography ----
    parsed = heuristic_parse(text)
    mentioned_cities = [c.title() for c in FR_CITIES if re.search(rf"\b{re.escape(c)}\b", folded)]
    removing = [
        c
        for c in mentioned_cities
        if re.search(
            rf"(retire\w*|enl[eè]ve\w*|remove|sans|except|hors|pas|exclu\w*|exclude|drop)\s+(?:la ville de\s+|les?\s+|the\s+)?(?:\w+\s+)?{re.escape(unidecode(c.lower()))}",
            folded,
        )
    ]
    adding = [c for c in mentioned_cities if c not in removing]
    if removing:
        am["remove_cities"] = removing
    if adding:
        if _ONLY.search(low) and not _ADD.search(low):
            am["remove_cities"] = [c for c in base.company_filters.cities if c not in adding] + removing
        am["add_cities"] = adding
    countries = [c for c in parsed.countries if c not in base.company_filters.countries]
    if countries and not (adding and countries == ["FR"]):
        am["add_countries"] = countries

    # ---- people ----
    if base.mode == CampaignMode.people:
        titles: list[str] = []
        for pat, ts, _fams in TITLE_WORDS:
            if pat.startswith("decision") and titles:
                continue
            if re.search(pat, low):
                titles += [x for x in ts if x not in titles]
        if "Founder" in titles and "Co-Founder" not in titles:
            titles.insert(titles.index("Founder") + 1, "Co-Founder")
        if titles:
            if _ADD.search(low) and not _ONLY.search(low):
                am["add_titles"] = [t for t in titles if t not in base.people_filters.titles]
            else:
                am["titles"] = titles

    # ---- size ----
    if parsed.employee_min is not None or parsed.employee_max is not None:
        am["employee_min"] = parsed.employee_min
        am["employee_max"] = parsed.employee_max

    # ---- email strictness ----
    if re.search(
        r"\b(safe only|only safe|uniquement safe|strictly verified|smtp[- ]verified|v[ée]rifi[ée]s? smtp|seulement (les )?(emails? )?v[ée]rifi[ée]s?)\b",
        low,
    ):
        am["accepted_email_statuses"] = [EmailStatus.SAFE]
    elif re.search(r"\b(risky|risqu[ée]s?)\b", low) and re.search(
        r"\b(ok|fine|accept\w*|aussi|also|too|inclu\w*)\b", low
    ):
        am["accepted_email_statuses"] = [EmailStatus.SAFE, EmailStatus.LIKELY_SAFE, EmailStatus.RISKY]
    elif re.search(r"\b(likely|probables?)\b", low):
        am["accepted_email_statuses"] = [EmailStatus.SAFE, EmailStatus.LIKELY_SAFE]

    # ---- industries (additive) ----
    if _ADD.search(low):
        inds = [i for i in _industries_from_text(text) if i not in base.company_filters.industries]
        if inds and not titles_only(low):
            am["add_industries"] = inds

    # ---- excluded keywords ----
    m = re.search(
        r"\b(?:exclu\w*|exclude|except|hors)\s+(?:les?\s+|the\s+|des\s+)?([a-zà-ÿ][a-zà-ÿ \-']{2,40})", low
    )
    if m:
        phrase = m.group(1).strip(" .,'")
        if (
            phrase
            and not any(phrase.startswith(unidecode(c.lower())) for c in mentioned_cities)
            and not re.search(r"e-?mails?|leads?|contacts?|personnes|people", phrase)
        ):
            am["exclude_keywords"] = [phrase]

    out = CampaignAmendment.model_validate(am)
    if out.model_copy(update={"instruction": None}).is_empty:
        raise ValidationFailed(
            "I couldn't map that change to the search criteria",
            code="amend_not_understood",
            hint="Try: “add Marseille too”, “founders only”, “+100 leads”, “2–30 employees” or “budget $10”.",
        )
    return out


def titles_only(low: str) -> bool:
    return bool(
        re.search(r"\b(founders?|fondat\w*|ceos?|cmo|cto|head of|directeur|dirigeant)\b", low)
    ) and not re.search(r"agenc|compan|entreprise|startup|soci", low)


def merge(parsed: CampaignAmendment | None, structured: CampaignAmendment) -> CampaignAmendment:
    """Structured fields (UI controls, LLM tool args) win over the parsed instruction."""
    if parsed is None:
        return structured
    data = parsed.model_dump()
    for k, v in structured.model_dump().items():
        if k == "instruction":
            continue
        if v not in (None, [], ""):
            data[k] = v
    data["instruction"] = structured.instruction or parsed.instruction
    return CampaignAmendment.model_validate(data)


# ---- apply + diff ---------------------------------------------------------------------------------------


def _fmt_list(v: list[str]) -> str:
    return ", ".join(v) if v else "—"


def _fmt_size(r: EmployeeRange | None) -> str:
    if r is None or (r.min is None and r.max is None):
        return "Any size"
    lo = r.min if r.min is not None else 0
    return f"{lo}–{r.max} employees" if r.max is not None else f"{lo}+ employees"


def _fmt_money(v: float | None) -> str:
    return "No limit" if v is None else f"${v:,.2f}"


def apply_amendment(
    base: CampaignDefinition, am: CampaignAmendment, *, qualified: int = 0
) -> tuple[CampaignDefinition, list[AmendmentChange], list[str]]:
    d = base.model_copy(deep=True)
    changes: list[AmendmentChange] = []
    warnings: list[str] = []
    cf = d.company_filters

    # target
    target = d.target_qualified_count
    if am.target_qualified_count is not None:
        target = am.target_qualified_count
    if am.add_target:
        target = (am.target_qualified_count or d.target_qualified_count) + am.add_target
    target = max(1, min(100_000, target))
    if target != d.target_qualified_count:
        changes.append(
            AmendmentChange(
                field="target",
                label="Target",
                before=f"{d.target_qualified_count:,}",
                after=f"{target:,}",
                safe=True,
            )
        )
        if target <= qualified:
            warnings.append(
                f"{qualified:,} leads are already qualified — the search will complete right away."
            )
        d.target_qualified_count = target

    # budget / runtime
    if am.max_cost_usd is not None and am.max_cost_usd != d.limits.max_cost_usd:
        changes.append(
            AmendmentChange(
                field="budget",
                label="Budget",
                before=_fmt_money(d.limits.max_cost_usd),
                after=_fmt_money(am.max_cost_usd),
                safe=True,
            )
        )
        d.limits.max_cost_usd = am.max_cost_usd
    if am.max_runtime_hours is not None and am.max_runtime_hours != d.limits.max_runtime_hours:
        changes.append(
            AmendmentChange(
                field="runtime",
                label="Max runtime",
                before=f"{d.limits.max_runtime_hours} h",
                after=f"{am.max_runtime_hours} h",
                safe=True,
            )
        )
        d.limits.max_runtime_hours = am.max_runtime_hours

    # geography
    before_loc = [*cf.cities, *[_country_name(c) for c in cf.countries]]
    cities = [c for c in cf.cities if c.lower() not in {x.lower() for x in am.remove_cities}]
    for c in am.add_cities:
        if c.lower() not in {x.lower() for x in cities}:
            cities.append(c)
    countries = list(cf.countries)
    for c in am.add_countries:
        if c.upper() not in countries:
            countries.append(c.upper()[:2])
    if am.add_cities and not countries:
        countries = ["FR"]
    if cities != cf.cities or countries != cf.countries:
        if cf.cities and not cities and am.remove_cities and not am.add_cities:
            warnings.append("No city left — the search widens to the whole country.")
        cf.cities, cf.countries = cities, countries
        changes.append(
            AmendmentChange(
                field="location",
                label="Location",
                before=_fmt_list(before_loc),
                after=_fmt_list([*cities, *[_country_name(c) for c in countries]]),
            )
        )

    # industries
    if am.add_industries:
        new = [i for i in am.add_industries if i not in cf.industries]
        if new:
            before = _fmt_list([i.capitalize() for i in cf.industries])
            cf.industries = [*cf.industries, *new]
            changes.append(
                AmendmentChange(
                    field="industries",
                    label="Companies",
                    before=before,
                    after=_fmt_list([i.capitalize() for i in cf.industries]),
                )
            )

    # size
    if am.employee_min is not None or am.employee_max is not None:
        rng = EmployeeRange(min=am.employee_min, max=am.employee_max)
        if _fmt_size(rng) != _fmt_size(cf.employee_range):
            changes.append(
                AmendmentChange(
                    field="size", label="Size", before=_fmt_size(cf.employee_range), after=_fmt_size(rng)
                )
            )
            cf.employee_range = rng

    # exclusions by keyword
    if am.exclude_keywords:
        new_kw = [k for k in am.exclude_keywords if k not in cf.exclude_keywords]
        if new_kw:
            before = _fmt_list(cf.exclude_keywords)
            cf.exclude_keywords = [*cf.exclude_keywords, *new_kw]
            changes.append(
                AmendmentChange(
                    field="exclude_keywords",
                    label="Exclude",
                    before=before,
                    after=_fmt_list(cf.exclude_keywords),
                )
            )

    # people
    if d.mode == CampaignMode.people:
        from scout.pipeline.icp import heuristic_parse

        pf = d.people_filters
        titles = list(pf.titles)
        if am.titles is not None:
            titles = list(dict.fromkeys(am.titles))
        for x in am.add_titles:
            if x not in titles:
                titles.append(x)
        if titles and titles != pf.titles:
            changes.append(
                AmendmentChange(
                    field="titles",
                    label="People",
                    before=" / ".join(pf.titles) or "—",
                    after=" / ".join(titles),
                )
            )
            pf.titles = titles
            fams = heuristic_parse("find " + ", ".join(titles)).role_families
            if fams:
                pf.role_families = fams
        if am.accepted_email_statuses is not None:
            new_st = list(dict.fromkeys(am.accepted_email_statuses))
            if set(new_st) != set(d.accepted_email_statuses):
                fmt = lambda xs: "/".join(s.value.replace("_", "-") for s in xs)  # noqa: E731
                changes.append(
                    AmendmentChange(
                        field="email", label="Email", before=fmt(d.accepted_email_statuses), after=fmt(new_st)
                    )
                )
                d.accepted_email_statuses = new_st
    elif am.titles or am.add_titles or am.accepted_email_statuses:
        warnings.append("This search finds companies only — people and email criteria don't apply.")

    if any(not c.safe for c in changes):
        warnings.append(
            "Leads already found stay in your list; new criteria apply to the companies analysed next."
        )
    return CampaignDefinition.model_validate(d.model_dump(mode="json")), changes, warnings
