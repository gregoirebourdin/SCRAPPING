"""Is this site alive, real and recently active?"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from ..fetch.crawler import CrawledSite, newest_date
from ..lexicon import PARKED_SIGNALS
from ..util.urls import is_blocked_domain

PARKING_HOSTS = re.compile(r"(sedo|dan\.com|afternic|hugedomains|godaddy|namecheap|parkingcrew|bodis|above\.com|undeveloped|squadhelp|brandbucket|domainmarket|uniregistry)", re.I)


@dataclass
class Liveness:
    alive: bool
    reason: str = "ok"
    last_activity: str | None = None
    copyright_year: int | None = None
    stale: bool = False
    details: dict = field(default_factory=dict)


def assess_liveness(site: CrawledSite, copyright_year: int | None = None) -> Liveness:
    d = dict(site.alive_details)
    if not site.alive:
        return Liveness(False, d.get("reason", "unreachable"), details=d)
    home = site.home
    if home is None:
        return Liveness(False, "no_home", details=d)
    title = home.parsed.title or ""
    head = f"{title}\n{home.text[:4000]}"
    if home.parsed.text_len < 150:
        return Liveness(False, "thin_content", details=d)
    if PARKED_SIGNALS.search(head) and home.parsed.text_len < 2500:
        return Liveness(False, "parked_or_placeholder", details=d)
    if site.redirected_domain and (is_blocked_domain(site.redirected_domain) or PARKING_HOSTS.search(site.redirected_domain)):
        return Liveness(False, f"redirect_to_{site.redirected_domain}", details=d)
    fu = (site.final_url or "").lower()
    if PARKING_HOSTS.search(fu) or re.search(r"/(suspended|expired|parked|forsale|for-sale)\b", fu):
        return Liveness(False, "parked_redirect", details=d)
    last = newest_date(site)
    now = datetime.utcnow()
    stale = False
    if last:
        try:
            age_days = (now - datetime.strptime(last[:10], "%Y-%m-%d")).days
        except ValueError:
            age_days = None
        if age_days is not None and age_days > 730 and (copyright_year or 0) < now.year - 1:
            stale = True
    elif copyright_year and copyright_year < now.year - 2:
        stale = True
    d.update({"last_activity": last, "copyright_year": copyright_year, "stale": stale, "sitemap_urls": len(site.sitemap_urls), "pages": len(site.pages)})
    return Liveness(True, "ok", last_activity=last, copyright_year=copyright_year, stale=stale, details=d)
