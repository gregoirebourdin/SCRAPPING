"""Free directory seeds reachable without a login or JS wall.

* Teachable Experts (static HTML, external links to agencies serving course creators)
* seed files dropped in ``data/seeds/*.txt`` (one domain/URL per line)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..config import settings
from ..fetch.http import Fetcher
from ..util.text import parse_html
from ..util.urls import absolutize, is_blocked_domain, looks_like_asset, registrable_domain, social_network

log = logging.getLogger(__name__)

TEACHABLE_EXPERTS = "https://www.teachable.com/experts"


async def teachable_experts(fetcher: Fetcher) -> list[tuple[str, str]]:
    """Return (url, label) pairs of external sites listed on Teachable Experts."""
    res = await fetcher.fetch(TEACHABLE_EXPERTS, ttl_hours=72)
    if not res.ok:
        return []
    page = parse_html(res.html, res.final_url)
    out: dict[str, tuple[str, str]] = {}
    for href, anchor in page.links:
        url = absolutize(page.url, href)
        if not url or looks_like_asset(url):
            continue
        dom = registrable_domain(url)
        if not dom or "teachable" in dom or is_blocked_domain(dom) or social_network(url) or dom.endswith(("calendly.com", "intellimize.co")):
            continue
        out.setdefault(dom, (url, anchor[:120]))
    return list(out.values())


def seed_files() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for f in sorted(Path(settings.data_dir, "seeds").glob("*.txt")):
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = re.split(r"[\s,;]+", line)[0]
            out.append((line, f"seed:{f.stem}"))
    return out
