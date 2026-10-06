"""Usage, cost (spec §95), modest analytics (spec §185) and internal diagnostics (spec §144)."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.models import Workspace


async def usage_summary(s: AsyncSession, workspace_id: uuid.UUID) -> dict[str, Any]:
    ws = await s.get(Workspace, workspace_id)
    month = (await s.execute(sa.text(
        "SELECT category, coalesce(sum(estimated_cost_usd),0) AS cost, coalesce(sum(quantity),0) AS qty, "
        "coalesce(sum(tokens_in),0) AS tin, coalesce(sum(tokens_out),0) AS tout FROM usage_events "
        "WHERE workspace_id = :ws AND created_at >= date_trunc('month', now()) GROUP BY category ORDER BY cost DESC"),
        {"ws": workspace_id})).all()
    by_model = (await s.execute(sa.text(
        "SELECT model, coalesce(sum(estimated_cost_usd),0), coalesce(sum(tokens_in),0), coalesce(sum(tokens_out),0), count(*) "
        "FROM usage_events WHERE workspace_id = :ws AND model IS NOT NULL AND created_at >= date_trunc('month', now()) "
        "GROUP BY model ORDER BY 2 DESC"), {"ws": workspace_id})).all()
    by_campaign = (await s.execute(sa.text(
        "SELECT c.id, c.name, st.cost_usd, st.qualified, st.raw_discovered FROM campaigns c JOIN campaign_stats st ON "
        "st.campaign_id = c.id WHERE c.workspace_id = :ws ORDER BY c.created_at DESC LIMIT 20"), {"ws": workspace_id})).all()
    daily = (await s.execute(sa.text(
        "SELECT date_trunc('day', created_at) AS d, coalesce(sum(estimated_cost_usd),0) FROM usage_events "
        "WHERE workspace_id = :ws AND created_at >= now() - interval '30 days' GROUP BY 1 ORDER BY 1"), {"ws": workspace_id})).all()
    qualified_month = await s.scalar(sa.text(
        "SELECT count(*) FROM lead_exposures WHERE workspace_id = :ws AND exposure_type = 'DISCOVERED' "
        "AND entity_type = 'person' AND occurred_at >= date_trunc('month', now())"), {"ws": workspace_id}) or 0
    total_cost = float(sum(float(r.cost) for r in month))
    totals = (await s.execute(sa.text(
        "SELECT (SELECT count(*) FROM companies WHERE workspace_id = :ws) AS companies, "
        "(SELECT count(*) FROM people WHERE workspace_id = :ws) AS people, "
        "(SELECT count(*) FROM companies WHERE workspace_id = :ws AND first_seen_at >= date_trunc('month', now())) AS new_companies, "
        "(SELECT count(*) FROM people WHERE workspace_id = :ws AND first_seen_at >= date_trunc('month', now())) AS new_people, "
        "(SELECT count(*) FROM emails WHERE workspace_id = :ws AND kind = 'person') AS emails, "
        "(SELECT count(*) FROM emails WHERE workspace_id = :ws AND kind = 'person' AND status = 'SAFE') AS safe_emails"),
        {"ws": workspace_id})).one()
    return {
        "month_cost_usd": round(total_cost, 4),
        "monthly_budget_usd": float(ws.monthly_budget_usd) if ws else None,
        "hard_budget_cap": ws.hard_budget_cap if ws else True,
        "qualified_this_month": int(qualified_month),
        "cost_per_qualified": round(total_cost / qualified_month, 4) if qualified_month else None,
        "by_category": [{"category": r.category, "cost_usd": float(r.cost), "quantity": int(r.qty), "tokens_in": int(r.tin),
                         "tokens_out": int(r.tout)} for r in month],
        "by_model": [{"model": m, "cost_usd": float(c), "tokens_in": int(i), "tokens_out": int(o), "calls": int(n)} for m, c, i, o, n in by_model],
        "by_campaign": [{"id": i, "name": n, "cost_usd": float(c), "qualified": q, "raw": r,
                         "yield": round(q / r, 3) if r else None, "cost_per_qualified": round(float(c) / q, 4) if q else None}
                        for i, n, c, q, r in by_campaign],
        "daily": [{"day": d, "cost_usd": float(c)} for d, c in daily],
        "totals": {
            "unique_companies": totals.companies, "unique_people": totals.people, "new_companies_this_month": totals.new_companies,
            "new_people_this_month": totals.new_people,
            "safe_email_rate": round(totals.safe_emails / totals.emails, 3) if totals.emails else None,
        },
    }


async def diagnostics(s: AsyncSession, workspace_id: uuid.UUID, *, hours: int = 24) -> dict[str, Any]:
    p = {"ws": workspace_id, "h": hours}
    crawl = (await s.execute(sa.text(
        "SELECT count(*) AS runs, count(*) FILTER (WHERE status = 'ok') AS ok, coalesce(sum(pages_fetched),0) AS pages, "
        "count(*) FILTER (WHERE tier_max <> 'http') AS browser FROM website_crawl_runs WHERE workspace_id = :ws "
        "AND started_at >= now() - make_interval(hours => :h)"), p)).one()
    cand = (await s.execute(sa.text(
        "SELECT count(*) AS total, count(*) FILTER (WHERE outcome = 'qualified') AS qualified, "
        "count(*) FILTER (WHERE outcome = 'duplicate') AS dup, count(*) FILTER (WHERE outcome = 'excluded_previous') AS excl, "
        "count(*) FILTER (WHERE stage IN ('people','email','deliver')) AS reached_people FROM company_discovery_events "
        "WHERE workspace_id = :ws AND observed_at >= now() - make_interval(hours => :h)"), p)).one()
    emails = (await s.execute(sa.text(
        "SELECT count(*) AS n, count(*) FILTER (WHERE status = 'SAFE') AS safe, count(*) FILTER (WHERE catch_all) AS catch_all "
        "FROM emails WHERE workspace_id = :ws AND created_at >= now() - make_interval(hours => :h)"), p)).one()
    ai = (await s.execute(sa.text(
        "SELECT count(*) FILTER (WHERE category = 'ai_tokens') AS calls, coalesce(sum(quantity) FILTER (WHERE category = 'grounded_search'),0) AS grounded, "
        "coalesce(sum(tokens_in + tokens_out),0) AS tokens, coalesce(sum(estimated_cost_usd),0) AS cost FROM usage_events "
        "WHERE workspace_id = :ws AND created_at >= now() - make_interval(hours => :h)"), p)).one()
    jobs = (await s.execute(sa.text(
        "SELECT error_category, count(*) FROM jobs WHERE workspace_id = :ws AND status IN ('failed','dead_letter','retrying') "
        "AND updated_at >= now() - make_interval(hours => :h) GROUP BY error_category"), p)).all()
    qualified = cand.qualified or 0
    minutes = hours * 60

    def pct(a: int, b: int) -> float | None:
        return round(100 * a / b, 1) if b else None

    return {
        "window_hours": hours,
        "companies_per_min": round((cand.total or 0) / minutes, 3),
        "pages_per_min": round((crawl.pages or 0) / minutes, 3),
        "crawl_success_pct": pct(crawl.ok, crawl.runs),
        "browser_fallback_pct": pct(crawl.browser, crawl.runs),
        "people_found_pct": pct(cand.reached_people, cand.total),
        "emails_found": emails.n, "safe_email_pct": pct(emails.safe, emails.n), "catch_all_pct": pct(emails.catch_all or 0, emails.n),
        "qualification_pct": pct(qualified, cand.total), "duplicates_pct": pct(cand.dup, cand.total),
        "previously_seen_exclusions_pct": pct(cand.excl, cand.total),
        "gemini_calls": ai.calls, "grounded_searches": int(ai.grounded), "tokens": int(ai.tokens),
        "cost_usd": float(ai.cost), "cost_per_qualified": round(float(ai.cost) / qualified, 4) if qualified else None,
        "job_failures": {str(k or "unknown"): n for k, n in jobs},
    }
