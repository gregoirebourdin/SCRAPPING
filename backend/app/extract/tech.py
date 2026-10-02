"""Technology / platform fingerprinting from raw HTML."""

from __future__ import annotations

from ..lexicon import FUNNEL_PLATFORMS, TECH_RE


def detect_tech(html: str, *, limit: int = 600_000) -> list[str]:
    sample = html[:limit]
    found = [name for name, rx in TECH_RE.items() if rx.search(sample)]
    # wordpress + elementor etc. are fine together; drop the generic "wordpress" when a page builder is identified
    return found


def primary_platform(tech: list[str]) -> str | None:
    """Pick the platform most likely hosting the funnel page itself."""
    order = ["clickfunnels", "kajabi", "gohighlevel", "kartra", "systeme.io", "leadpages", "funnelish", "groovefunnels", "unbounce", "instapage",
             "samcart", "thrivecart", "teachable", "thinkific", "podia", "stan store", "skool", "hubspot", "webflow", "framer", "elementor", "wordpress", "squarespace", "wix"]
    for name in order:
        if name in tech and name in FUNNEL_PLATFORMS:
            return name
    return None
