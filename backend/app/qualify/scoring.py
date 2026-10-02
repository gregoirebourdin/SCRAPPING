"""ICP scoring: is this an *acquisition* agency that serves *coaches / infopreneurs*, and how strong is the proof?

Score components (max 100):
  services        25  acquisition services sold (meta/google/youtube ads, funnels, lead gen, launches...)
  icp             25  coaches / course creators / info products / high ticket / memberships / webinars
  client_proof    25  named coach/infopreneur client(s) with evidence
  agency          10  agency-ness (case studies, "our clients", booking link, team...)
  freshness       10  recently active site
  contact          5  reachable email
  penalties       -   SaaS / job board / listicle / directory / course platform / stale / thin
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import settings
from ..extract.clients import ClientHit
from ..extract.contacts import Contacts
from ..fetch.crawler import CrawledSite
from ..lexicon import ACQUISITION_LABELS, AGENCY_SIGNALS, CORE_ICP_LABELS, ICP, NON_AGENCY_SIGNALS, SERVICES
from .liveness import Liveness

_SERVICE_RE = {k: re.compile("|".join(v), re.I) for k, v in SERVICES.items()}
_ICP_RE = {k: re.compile("|".join(v), re.I) for k, v in ICP.items()}
_AGENCY_RE = [re.compile(p, re.I) for p in AGENCY_SIGNALS]
_NON_AGENCY_RE = [(re.compile(p, re.I), label) for p, label in NON_AGENCY_SIGNALS]
_FREELANCE_RE = re.compile(r"\b(freelance(r)?|i'?m a (media buyer|marketer|consultant)|hire me|my services|work with me)\b", re.I)


@dataclass
class ScoreResult:
    score: int
    status: str  # qualified | review | rejected
    tier: str | None
    reject_reason: str | None
    services: list[str]
    icp_signals: list[str]
    breakdown: dict = field(default_factory=dict)


def _count(rx: re.Pattern, text: str, cap: int = 30) -> int:
    n = 0
    for _ in rx.finditer(text):
        n += 1
        if n >= cap:
            break
    return n


def score_agency(
    site: CrawledSite,
    liveness: Liveness,
    language: tuple[str, float],
    clients: list[ClientHit],
    contacts: Contacts,
    *,
    min_score: int | None = None,
) -> ScoreResult:
    min_score = settings.min_lead_score if min_score is None else min_score
    home = site.home
    core_pages = site.pages_of("home", "services", "industries", "about", "clients", "case_studies")
    core_text = "\n".join(p.text for p in core_pages)[:150_000]
    headline = " ".join(
        [home.parsed.title, home.parsed.meta_description, home.parsed.og_title] + home.parsed.headings[:12]
    ) if home else ""
    kchars = max(1.0, len(core_text) / 1000)
    breakdown: dict = {}

    # --- hard gates --------------------------------------------------------------------------------------------
    code, conf = language
    if not liveness.alive:
        return ScoreResult(0, "rejected", None, f"dead:{liveness.reason}", [], [], {"gate": "liveness"})
    if not (code == "en" and conf >= settings.english_min_confidence):
        return ScoreResult(0, "rejected", None, f"language:{code}:{conf:.2f}", [], [], {"gate": "language"})

    # --- services ----------------------------------------------------------------------------------------------
    svc_hits = {k: _count(rx, core_text) for k, rx in _SERVICE_RE.items()}
    services = [k for k, n in sorted(svc_hits.items(), key=lambda kv: -kv[1]) if n > 0]
    acq = [s for s in services if s in ACQUISITION_LABELS]
    acq_strength = sum(min(8, svc_hits[s]) for s in acq)
    services_score = min(25, len(acq) * 4 + acq_strength * 0.6)
    if any(_SERVICE_RE[s].search(headline) for s in acq):
        services_score = min(25, services_score + 6)
    breakdown["services"] = round(services_score, 1)
    breakdown["service_hits"] = {k: v for k, v in svc_hits.items() if v}

    # --- ICP ----------------------------------------------------------------------------------------------------
    icp_hits = {k: _count(rx, core_text) for k, rx in _ICP_RE.items()}
    icp_signals = [k for k, n in sorted(icp_hits.items(), key=lambda kv: -kv[1]) if n > 0]
    core_icp = [k for k in icp_signals if k in CORE_ICP_LABELS]
    density = sum(icp_hits[k] for k in core_icp) / kchars
    icp_score = min(25, len(core_icp) * 3.5 + min(12, density * 2.5))
    headline_icp = [k for k in CORE_ICP_LABELS if _ICP_RE[k].search(headline)]
    if headline_icp:
        icp_score = min(25, icp_score + 8)
    # "coaches" alone can be sports/athletics; demand a second infopreneur signal or an explicit online context
    if core_icp == ["coaches"] and not re.search(r"\b(online|digital|program|course|webinar|funnel|high[- ]ticket|clients|calls)\b", core_text, re.I):
        icp_score *= 0.4
    breakdown["icp"] = round(icp_score, 1)
    breakdown["icp_hits"] = {k: v for k, v in icp_hits.items() if v}
    breakdown["headline_icp"] = headline_icp

    # --- client proof -------------------------------------------------------------------------------------------
    coach_clients = [c for c in clients if c.role_title or re.search(r"\b(coach|course|program|students|launch|webinar|mastermind|membership|academy)\b", c.evidence, re.I)]
    best = max((c.confidence for c in coach_clients), default=0.0)
    proof = best * 20 + (5 if len(coach_clients) >= 2 else 0)
    client_score = min(25, proof)
    breakdown["client_proof"] = round(client_score, 1)
    breakdown["coach_clients"] = len(coach_clients)

    # --- agency-ness ---------------------------------------------------------------------------------------------
    ag = sum(1 for rx in _AGENCY_RE if rx.search(core_text))
    agency_score = min(10, ag * 1.5 + (2 if site.pages_of("case_studies") else 0) + (1.5 if contacts.booking_url else 0) + (1 if site.pages_of("clients") else 0))
    breakdown["agency"] = round(agency_score, 1)

    # --- freshness -----------------------------------------------------------------------------------------------
    if liveness.last_activity:
        year = int(liveness.last_activity[:4])
        from datetime import datetime

        age = datetime.utcnow().year - year
        fresh = 10 if age <= 1 else 6 if age == 2 else 2
    else:
        fresh = 4
    if liveness.stale:
        fresh = 0
    breakdown["freshness"] = fresh

    # --- contact -------------------------------------------------------------------------------------------------
    contact = 5 if any(e.same_domain for e in contacts.emails) else 3 if contacts.emails else 0
    breakdown["contact"] = contact

    # --- penalties -----------------------------------------------------------------------------------------------
    penalties = 0
    flags: list[str] = []
    for rx, label in _NON_AGENCY_RE:
        n = _count(rx, core_text, cap=5)
        if n and label in ("saas", "course_platform") and n >= 2 and not acq:
            penalties += 15
            flags.append(label)
        elif n and label in ("job_board", "directory"):
            penalties += 15
            flags.append(label)
        elif n and label == "listicle" and not re.search(r"\bour (clients|services|team)\b", core_text, re.I):
            penalties += 20
            flags.append(label)
    if _FREELANCE_RE.search(core_text) and not re.search(r"\b(our team|we are a team|agency)\b", core_text, re.I):
        penalties += 5
        flags.append("solo")
    blog_ratio = len(site.pages_of("blog")) / max(1, len(site.pages))
    if home and home.parsed.text_len > 25_000 and not acq:
        penalties += 10
        flags.append("content_site")
    if blog_ratio > 0.5:
        penalties += 5
        flags.append("blog_heavy")
    breakdown["penalties"] = -penalties
    breakdown["flags"] = flags

    score = int(round(max(0, min(100, services_score + icp_score + client_score + agency_score + fresh + contact - penalties))))
    breakdown["total"] = score

    # --- verdict -------------------------------------------------------------------------------------------------
    reason = None
    if not acq:
        reason = "no_acquisition_services"
    elif not core_icp:
        reason = "no_infopreneur_icp"
    elif "listicle" in flags or "directory" in flags or "job_board" in flags:
        reason = "not_an_agency:" + ",".join(flags)
    elif icp_score < 5:
        reason = "weak_icp"

    has_email = bool(contacts.emails)
    has_coach_client = best >= 0.5
    if reason:
        status = "rejected" if score < min_score - 15 else "review"
        tier = None if status == "rejected" else "C"
    elif score >= min_score:
        status = "qualified"
        tier = "A" if has_coach_client and has_email else "B" if has_email else "C"
        if settings.require_named_client and not has_coach_client:
            status = "qualified" if tier == "B" else "review"
    elif score >= min_score - 15:
        status, tier = "review", "C"
    else:
        status, tier, reason = "rejected", None, "low_score"
    breakdown["tier_inputs"] = {"has_email": has_email, "has_coach_client": has_coach_client, "best_client_conf": round(best, 2)}
    return ScoreResult(score, status, tier, reason, services, icp_signals, breakdown)
