"""Website candidates from free web search, for website resolution of registry / maps candidates without a URL.

``search_domain_candidates("Pixel Studio", city="Lyon", country="FR")`` → registrable company domains of results
that name the company (directories, social networks, media, gov/edu and listicles dropped), best first.
They are *candidates only*: ``scout.crawl.resolve.resolve_website`` must still prove identity on the home page
(SIREN / phone / name + city) exactly as for its DNS-guessed candidates, so nothing is accepted on search
evidence alone. No-op ([]) without a healthy lookup provider (SearXNG) — no Gemini involved.
"""

from __future__ import annotations

from scout.search.assess import is_listicle, mentions_subject
from scout.search.chain import SearchChain, lookup_chain


async def search_domain_candidates(
    name: str,
    *,
    city: str | None = None,
    country: str | None = None,
    limit: int = 5,
    chain: SearchChain | None = None,
) -> list[str]:
    from scout.discovery.common import candidate_domain

    if not name.strip():
        return []
    chain = chain or lookup_chain("website")
    if not chain.available():
        return []
    query = f'"{name.strip()}" {city or ""}'.strip()
    res = await chain.search(query, num=10, region=(country or "").upper() or None)
    out: list[str] = []
    for r in res.results:
        if is_listicle(r) or not mentions_subject(r, names=[name]):
            continue
        dom = candidate_domain(r.url)
        if dom and dom not in out:
            out.append(dom)
        if len(out) >= limit:
            break
    return out
