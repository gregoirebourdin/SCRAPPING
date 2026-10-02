"""Query matrix generation.

The matrix is ordered by expected yield so a capped run (``max_queries``) still starts with the best
queries: hand-written phrase queries first, then listicles, then the service × ICP product.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass

from ..lexicon import ICP_QUERY_TERMS, LISTICLE_QUERIES, PHRASE_QUERIES, SEARCH_MODIFIERS, SERVICE_QUERY_TERMS


@dataclass(frozen=True)
class Query:
    text: str
    kind: str  # phrase | listicle | matrix | modifier | extra
    priority: int


def build_queries(extra: list[str] | None = None, *, seed: int = 7) -> list[Query]:
    out: list[Query] = []
    seen: set[str] = set()

    def add(text: str, kind: str, prio: int) -> None:
        t = " ".join(text.split()).strip()
        if t.lower() in seen:
            return
        seen.add(t.lower())
        out.append(Query(t, kind, prio))

    for q in extra or []:
        add(q, "extra", 0)
    for q in PHRASE_QUERIES:
        add(q, "phrase", 1)
    for q in LISTICLE_QUERIES:
        add(q, "listicle", 1)

    rnd = random.Random(seed)
    combos = list(itertools.product(SERVICE_QUERY_TERMS, ICP_QUERY_TERMS))
    rnd.shuffle(combos)
    # most specific services first
    hot = {"facebook ads agency", "meta ads agency", "funnel agency", "paid ads agency", "youtube ads agency", "lead generation agency", "sales funnel agency", "launch agency", "webinar funnel agency", "growth partner", "done for you ads", "ads management for"}
    combos.sort(key=lambda c: 0 if c[0] in hot else 1)
    for service, icp in combos:
        text = f"{service} {icp}" if service.endswith(("for", "management for")) else f"{service} for {icp}"
        add(text, "matrix", 2)

    for service, icp in combos[:120]:
        for mod in SEARCH_MODIFIERS[:3]:
            add(f"{service} for {icp} {mod}", "modifier", 3)
    return out
