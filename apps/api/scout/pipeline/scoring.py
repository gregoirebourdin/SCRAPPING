"""ICP scoring (spec §72), separate confidences (spec §73) and the quality gate (spec §74).

Components that were not requested/collected are excluded from the denominator instead of counting as 0;
the explanation says so. Explanations are concise evidence bullets, never model chain-of-thought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scout.db.enums import CampaignMode, EmailStatus
from scout.schemas.campaign import CampaignDefinition


@dataclass
class ConditionOutcome:
    label: str
    passed: bool | None
    confidence: float
    evidence: str | None = None
    source_url: str | None = None
    required: bool = True


@dataclass
class ScoringInput:
    defn: CampaignDefinition
    industry_fit: float | None  # 0–1 (None = unknown)
    size_fit: float | None
    location_fit: float | None
    company_confidence: float | None
    conditions: list[ConditionOutcome] = field(default_factory=list)
    # person
    person_identified: bool = False
    person_name: str | None = None
    person_title: str | None = None
    title_match: float | None = None  # 0–1
    decision_power: int | None = None
    person_confidence: float | None = None
    person_source: str | None = None
    # email
    email: str | None = None
    email_status: EmailStatus | None = None
    email_confidence: float | None = None  # 0–1
    phone: bool = False
    profile_url: bool = False
    # intent / evidence
    signals: list[dict[str, Any]] = field(default_factory=list)
    evidence_sources: int = 0
    evidence_quality: float | None = None  # 0–1 average source quality
    excluded: bool = False
    suppressed: bool = False
    company_name: str | None = None
    industry_label: str | None = None


@dataclass
class ScoreResult:
    icp_score: int
    company_fit: float | None
    person_fit: float | None
    intent: float | None
    contactability: float | None
    evidence: float | None
    company_confidence: float | None
    person_confidence: float | None
    email_confidence: float | None
    enrichment_confidence: float | None
    overall_confidence: float | None
    qualified: bool
    gates: list[dict[str, Any]]
    weights: dict[str, float]
    explanation: list[str]
    first_failure: str | None


def _avg(vals: list[float | None]) -> float | None:
    xs = [v for v in vals if v is not None]
    return sum(xs) / len(xs) if xs else None


def company_fit(inp: ScoringInput) -> float | None:
    parts: list[tuple[float, float]] = []  # (weight, value)
    if inp.industry_fit is not None:
        parts.append((0.45, inp.industry_fit))
    if inp.size_fit is not None:
        parts.append((0.25, inp.size_fit))
    if inp.location_fit is not None:
        parts.append((0.2, inp.location_fit))
    conds = [c for c in inp.conditions if c.passed is not None]
    if conds:
        parts.append((0.1, sum(1.0 if c.passed else 0.0 for c in conds) / len(conds)))
    if not parts:
        return None
    w = sum(p[0] for p in parts)
    return sum(p[0] * p[1] for p in parts) / w


def score(inp: ScoringInput) -> ScoreResult:
    d = inp.defn
    W = d.score_weights
    people_mode = d.mode == CampaignMode.people
    comp: dict[str, float | None] = {
        "company_fit": company_fit(inp),
        "person_fit": None,
        "intent": None,
        "contactability": None,
        "evidence": None,
    }
    if people_mode and inp.person_identified:
        tm = inp.title_match if inp.title_match is not None else 0.5
        dp = (inp.decision_power or 50) / 100.0
        pc = inp.person_confidence or 0.0
        comp["person_fit"] = 0.55 * tm + 0.25 * dp + 0.2 * pc
    if inp.signals:
        comp["intent"] = min(1.0, sum(float(sg.get("confidence", 0.5)) for sg in inp.signals) / 2.0)
    contact_parts = []
    if people_mode:
        if inp.email_status is not None:
            base = {
                EmailStatus.SAFE: 1.0,
                EmailStatus.RISKY: 0.6,
                EmailStatus.CATCH_ALL: 0.4,
                EmailStatus.UNKNOWN: 0.25,
                EmailStatus.INVALID: 0.0,
            }[inp.email_status]
            contact_parts.append(base * (0.5 + 0.5 * (inp.email_confidence or 0.0)))
        else:
            contact_parts.append(0.0)
    if inp.phone:
        contact_parts.append(0.7)
    if inp.profile_url:
        contact_parts.append(0.6)
    if contact_parts:
        comp["contactability"] = min(1.0, max(contact_parts) + 0.1 * (len(contact_parts) - 1))
    if inp.evidence_sources:
        comp["evidence"] = min(
            1.0, 0.5 * min(inp.evidence_sources, 3) / 3 + 0.5 * (inp.evidence_quality or 0.6)
        )
    weights = {
        "company_fit": W.company_fit,
        "person_fit": W.person_fit if people_mode else 0.0,
        "intent": W.intent,
        "contactability": W.contactability,
        "evidence": W.evidence,
    }
    num = den = 0.0
    used: dict[str, float] = {}
    for k, w in weights.items():
        v = comp[k]
        if v is None or w <= 0:
            continue
        num += w * v
        den += w
        used[k] = w
    icp = round(100 * num / den) if den else 0
    email_conf = inp.email_confidence
    enrich_conf = _avg([c.confidence for c in inp.conditions if c.passed is not None])
    overall = _avg(
        [
            inp.company_confidence,
            inp.person_confidence if people_mode else None,
            email_conf if people_mode else None,
            enrich_conf,
        ]
    )
    # ---------------- gates ----------------
    gates: list[dict[str, Any]] = []

    def gate(name: str, ok: bool, reason: str) -> None:
        gates.append({"gate": name, "passed": ok, "reason": None if ok else reason})

    cf = comp["company_fit"]
    # No company criterion requested (e.g. a list-seeded campaign) or none could be evaluated: nothing to fail —
    # consistent with the company_qualification stage, and never scored as a zero.
    gate(
        "company_fit",
        cf is None or cf * 100 >= d.minimum_company_fit,
        f"Company fit {round((cf or 0) * 100)} < {d.minimum_company_fit}",
    )
    for c in inp.conditions:
        if c.required:
            gate(
                f"condition:{c.label}",
                c.passed is True,
                f"{c.label}: " + ("not satisfied" if c.passed is False else "insufficient evidence"),
            )
    if people_mode and d.requires_person:
        gate("person_identified", inp.person_identified, "No decision maker identified")
        if d.people_filters.titles or d.people_filters.role_families:
            gate(
                "person_role",
                (inp.title_match or 0) >= 0.5,
                f"Role '{inp.person_title or 'unknown'}' does not match",
            )
        gate(
            "person_confidence",
            (inp.person_confidence or 0) * 100 >= d.minimum_person_confidence,
            f"Person confidence {round((inp.person_confidence or 0) * 100)} < {d.minimum_person_confidence}",
        )
    if people_mode and d.requires_email:
        gate("email_exists", bool(inp.email), "No professional email found")
        gate(
            "email_status",
            inp.email_status in d.accepted_email_statuses,
            f"Email status {inp.email_status.value if inp.email_status else 'none'} not accepted",
        )
        gate(
            "email_confidence",
            (inp.email_confidence or 0) * 100 >= d.minimum_email_confidence,
            f"Email confidence {round((inp.email_confidence or 0) * 100)} < {d.minimum_email_confidence}",
        )
    gate("not_excluded", not inp.excluded, "Previously seen (exclusion active)")
    gate("not_suppressed", not inp.suppressed, "Suppressed")
    gate("icp_score", icp >= d.minimum_icp_score, f"ICP score {icp} < {d.minimum_icp_score}")
    qualified = all(g["passed"] for g in gates)
    first_failure = next((g["reason"] for g in gates if not g["passed"]), None)
    # ---------------- explanation ----------------
    expl: list[str] = []
    if cf is None:
        expl.append("No company criteria requested (company fit not scored)")
    if inp.industry_fit is not None:
        expl.append(
            ("Correct industry" if inp.industry_fit >= 0.7 else "Partial industry match")
            + (f" ({inp.industry_label})" if inp.industry_label else "")
        )
    if inp.size_fit is not None and inp.size_fit >= 0.7:
        expl.append("Company size in range")
    elif inp.size_fit is None and d.company_filters.employee_range:
        expl.append("Company size unknown (not penalized as a mismatch)")
    for c in inp.conditions:
        if c.passed:
            expl.append(f"{c.label} confirmed" + (" on website" if c.source_url else ""))
    if people_mode and inp.person_identified:
        expl.append(
            f"{inp.person_title or 'Decision maker'} identified"
            + (f" via {inp.person_source}" if inp.person_source else "")
        )
    if inp.email_status is not None:
        expl.append(f"{inp.email_status.value.replace('_', '-')} email")
    for sg in inp.signals[:3]:
        expl.append(f"Signal: {sg.get('type', 'signal').replace('_', ' ')}")
    if "intent" not in used:
        expl.append("No intent signals collected (excluded from score)")
    return ScoreResult(
        icp_score=icp,
        company_fit=_r(cf),
        person_fit=_r(comp["person_fit"]),
        intent=_r(comp["intent"]),
        contactability=_r(comp["contactability"]),
        evidence=_r(comp["evidence"]),
        company_confidence=_r(inp.company_confidence),
        person_confidence=_r(inp.person_confidence),
        email_confidence=_r(email_conf),
        enrichment_confidence=_r(enrich_conf),
        overall_confidence=_r(overall),
        qualified=qualified,
        gates=gates,
        weights=used,
        explanation=expl,
        first_failure=first_failure,
    )


def _r(v: float | None) -> float | None:
    return None if v is None else round(float(v), 3)


def size_fit(defn: CampaignDefinition, emp_min: int | None, emp_max: int | None) -> float | None:
    er = defn.company_filters.employee_range
    if er is None or (er.min is None and er.max is None):
        return None
    if emp_min is None and emp_max is None:
        return None
    lo = emp_min if emp_min is not None else emp_max
    hi = emp_max if emp_max is not None else emp_min
    assert lo is not None and hi is not None
    want_lo = er.min if er.min is not None else 0
    want_hi = er.max if er.max is not None else 10**9
    if hi < want_lo or lo > want_hi:
        # distance-based partial credit for near misses (e.g. 31 employees for 2–30)
        gap = (want_lo - hi) if hi < want_lo else (lo - want_hi)
        return 0.3 if gap <= max(2, 0.15 * max(want_hi if want_hi < 10**9 else want_lo, 1)) else 0.0
    return 1.0


def location_fit(
    defn: CampaignDefinition, country: str | None, city: str | None, region: str | None
) -> float | None:
    cf = defn.company_filters
    if not (cf.countries or cf.cities or cf.regions):
        return None
    if cf.cities:
        if city and any(city.strip().lower() == c.strip().lower() for c in cf.cities):
            return 1.0
        if city is None:
            return None
        return 0.3 if (country and country in cf.countries) else 0.0
    if cf.countries:
        if country is None:
            return None
        return 1.0 if country.upper() in cf.countries else 0.0
    return None
