"""Campaign debrief: what happened (funnel, why candidates were rejected, which sources produced) and concrete
alternative strategies, each with an instruction the assistant can run as-is (amend the search or plan a new one).

The strategies are derived from the evidence (rejection reasons, source yields), never invented: a search that
fails on "Website conditions" gets "target the likely users instead"; one that fails on locations gets "widen the
area", one blocked by email strictness gets "accept RISKY or verify", etc. The model phrases and adapts them.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import CandidateOutcome
from scout.db.models import Campaign, CampaignSource, CampaignStats, CompanyDiscoveryEvent
from scout.schemas.campaign import CampaignDefinition

# reason prefix → bucket
_BUCKETS: tuple[tuple[str, str], ...] = (
    ("Website not found", "no_website"),
    ("Website unreachable", "no_website"),
    ("Website blocks", "no_website"),
    ("Parked domain", "no_website"),
    ("Location outside", "location"),
    ("Location not confirmed", "location"),
    ("Country ", "location"),
    ("Not a ", "business_type"),
    ("Company fit too low", "business_type"),
    ("Company size", "size"),
    ("No decision maker", "people"),
    ("No qualified decision maker", "people"),
    ("Role '", "people"),
    ("Email status", "email"),
    ("Email confidence", "email"),
    ("No professional email", "email"),
    ("Suppressed", "suppressed"),
    ("Excluded", "excluded"),
    ("Same company already", "duplicate"),
)


def bucket(reason: str | None, stage: str | None) -> str:
    r = reason or ""
    if stage == "website_conditions":
        return "website_condition"
    for prefix, b in _BUCKETS:
        if r.startswith(prefix):
            return b
    return "other"


def _condition_text(c: Any) -> str:
    return str(
        getattr(c, "label", None)
        or getattr(c, "concept", None)
        or " / ".join(getattr(c, "terms", None) or getattr(c, "technologies", None) or [])
        or getattr(c, "pattern", "")
    )


def strategies(
    defn: CampaignDefinition, buckets: Counter[str], qualified: int, target: int
) -> list[dict[str, Any]]:
    """Evidence-based next moves, most impactful first (≤ 4). ``action``: amend | plan | column | info."""
    total = sum(buckets.values()) or 1
    share = {k: v / total for k, v in buckets.items()}
    cf = defn.company_filters
    out: list[dict[str, Any]] = []
    conds = [_condition_text(c) for c in defn.website_conditions]
    if share.get("website_condition", 0) >= 0.25 and conds:
        out.append(
            {
                "title": "Target the likely users instead of the mention",
                "why": f"{buckets['website_condition']} companies were dropped because their website never says "
                f"“{conds[0]}”: tools and suppliers are rarely named publicly.",
                "action": "plan",
                "instruction": f"Find the companies most likely to use/need “{conds[0]}”: the industries and job "
                "functions that typically do, checked on their website for the matching activity",
            }
        )
        out.append(
            {
                "title": "Keep the companies, check the signal as a column",
                "why": "Searching on the activity first and verifying the signal afterwards keeps the leads you would "
                "otherwise lose.",
                "action": "amend",
                "instruction": "drop the website condition; I'll add it as an enrichment column afterwards",
            }
        )
    if share.get("no_website", 0) >= 0.4:
        out.append(
            {
                "title": "Use sources that know the company's website",
                "why": f"{buckets['no_website']} candidates had no findable website (registry entries, sole traders).",
                "action": "info",
                "instruction": "Connect Google Places (free tier) so local businesses come with their site; meanwhile "
                "widen the search terms so web results carry more company sites",
            }
        )
    if share.get("location", 0) >= 0.2 and cf.cities:
        out.append(
            {
                "title": "Widen the area",
                "why": f"{buckets['location']} companies were just outside {', '.join(cf.cities)}.",
                "action": "amend",
                "instruction": f"include the surroundings of {', '.join(cf.cities)} (nearby towns)",
            }
        )
    if share.get("business_type", 0) >= 0.2:
        out.append(
            {
                "title": "Sharpen or broaden the business type",
                "why": f"{buckets['business_type']} companies turned out to do something else than requested.",
                "action": "amend",
                "instruction": "add the closest neighbouring business types (synonyms / specialties)",
            }
        )
    if share.get("email", 0) >= 0.15:
        out.append(
            {
                "title": "Accept unverified (RISKY) emails, clearly labelled",
                "why": f"{buckets['email']} decision makers were found but their email could not be confirmed.",
                "action": "amend",
                "instruction": "accept RISKY emails too",
            }
        )
    if share.get("people", 0) >= 0.2:
        out.append(
            {
                "title": "Accept other decision makers",
                "why": f"{buckets['people']} companies had no one with the requested title on record.",
                "action": "amend",
                "instruction": "also accept managers and directors (not only founders / CEOs)",
            }
        )
    if qualified >= target:
        out.append(
            {
                "title": "Go further with these leads",
                "why": "The target is reached.",
                "action": "amend",
                "instruction": f"+{max(20, target)} leads",
            }
        )
    return out[:4]


async def build_debrief(s: AsyncSession, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> dict[str, Any]:
    c = await s.get(Campaign, campaign_id)
    if c is None or c.workspace_id != workspace_id:
        raise ValueError("campaign not found")
    st = await s.get(CampaignStats, campaign_id)
    defn = CampaignDefinition.model_validate(c.definition)
    rows = (
        await s.execute(
            sa.select(CompanyDiscoveryEvent.stage, CompanyDiscoveryEvent.reason, sa.func.count())
            .where(
                CompanyDiscoveryEvent.campaign_id == campaign_id,
                CompanyDiscoveryEvent.outcome.in_([CandidateOutcome.rejected, CandidateOutcome.error]),
            )
            .group_by(CompanyDiscoveryEvent.stage, CompanyDiscoveryEvent.reason)
        )
    ).all()
    buckets: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for stage, reason, n in rows:
        buckets[bucket(reason, stage)] += int(n)
        key = (reason or stage or "other").split(":")[0][:80]
        reasons[key] += int(n)
    sources = [
        {
            "source": x.source_key,
            "status": x.status.value,
            "found": x.raw_count,
            "new": x.unique_count,
            "error": (x.last_error or "")[:160] or None,
        }
        for x in (
            await s.scalars(sa.select(CampaignSource).where(CampaignSource.campaign_id == campaign_id))
        ).all()
    ]
    qualified = st.qualified if st else 0
    funnel = {
        "discovered": st.raw_discovered if st else 0,
        "analysed": st.companies_evaluated if st else 0,
        "matched": st.companies_matched if st else 0,
        "people_found": st.people_found if st else 0,
        "qualified": qualified,
        "target": c.target_qualified_count,
    }
    return {
        "campaign_id": str(c.id),
        "name": c.name,
        "status": c.status.value,
        "stop_reason": c.stop_reason,
        "funnel": funnel,
        "top_reasons": [{"reason": r, "count": n} for r, n in reasons.most_common(6)],
        "blockers": dict(buckets.most_common()),
        "sources": sources,
        "strategies": strategies(defn, buckets, qualified, c.target_qualified_count),
    }
