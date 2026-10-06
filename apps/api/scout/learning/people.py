"""People sources: ``people.source`` keys, learned confidences and attempt bookkeeping for one company.

Keys describe *where* a decision maker was found (the user's notion of a source): ``registry``,
``legal_notice``, ``official_team_page``, ``about_page``, ``home_page``, ``contact_page``, ``website_other``,
``ai_extraction``, ``gemini_grounded_result``, ``searxng_result``, ``directory``, ``linkedin_snippet``,
``import``, ``user``. The same key is derived at extraction time (from the candidate) and at feedback time
(from the stored observation: source type + page URL), so user corrections land on the source that produced
the value.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from typing import Any

import structlog

from scout.learning import routing
from scout.learning import stats as S

log = structlog.get_logger("learning.people")

DIMENSION = "people.source"
WEBSITE_PAGE_KEYS: dict[str, str] = {
    "team": "official_team_page",
    "about": "about_page",
    "home": "home_page",
    "contact": "contact_page",
    "legal": "legal_notice",
}


def _val(v: Any) -> str:
    return str(getattr(v, "value", v) or "")


def _classify(url: str | None) -> str | None:
    if not url:
        return None
    try:
        from scout.crawl.page_selector import classify_page_type

        return classify_page_type(url).value
    except Exception:
        return None


def page_key(page_type: str | None) -> str:
    return WEBSITE_PAGE_KEYS.get(page_type or "", "website_other")


def people_source_key(
    source_type: Any,
    *,
    page_type: Any = None,
    source_url: str | None = None,
    method: str | None = None,
) -> str:
    """``people.source`` key for a candidate or a stored person observation."""
    st, m, url = _val(source_type), method or "", (source_url or "").lower()
    if m == "registry" or st == "registry":
        return "registry"
    if m == "legal_notice":
        return "legal_notice"
    if m == "ai" or st == "ai_extraction":
        return "ai_extraction"
    if m == "grounded" or st == "grounded_search":
        return "gemini_grounded_result"
    if st in ("search_snippet", "public_profile"):
        if "linkedin." in url:
            return "linkedin_snippet"
        return "searxng_result" if st == "search_snippet" else "directory"
    if st in ("directory", "maps"):
        return "directory"
    if st in ("import", "user"):
        return st
    pt = _val(page_type) or _classify(source_url)
    return page_key(pt if pt and pt != "other" else None)


def candidate_key(c: Any) -> str:
    return people_source_key(
        getattr(c, "source_type", None),
        page_type=getattr(c, "page_type", None),
        source_url=getattr(c, "source_url", None),
        method=getattr(c, "method", None),
    )


class PeopleLearning:
    """Per-company helper used by the pipeline's people stage (``scout.pipeline.processor``).

    * ``adjust()`` blends each candidate's hard-coded confidence with the learned precision of its source
      (``routing.blend_confidence``) when ``empirical_routing_enabled``;
    * ``website()`` / ``step()`` collect attempts (produced or not, latency, cost); ``flush()`` records them.
    Never raises.
    """

    def __init__(self, snap: S.Snapshot | None = None, *, use: bool = True) -> None:
        self.snap = snap or {}
        self.use = use
        self.events: list[S.StatEvent] = []

    @classmethod
    async def start(cls) -> PeopleLearning:
        use = routing.enabled()
        snap = await S.snapshot() if use else {}
        return cls(snap, use=use)

    def adjust[T](self, cands: list[T]) -> list[T]:
        if not self.use or not self.snap:
            return cands
        for c in cands:
            try:
                conf = getattr(c, "confidence", None)
                if isinstance(conf, int | float):
                    learned = routing.learned_confidence(DIMENSION, candidate_key(c), float(conf), self.snap)
                    setattr(c, "confidence", learned)  # noqa: B010 - PersonCandidate-like objects
            except Exception as exc:  # pragma: no cover - defensive
                log.info("learning.people_adjust_failed", error=str(exc))
        return cands

    def website(self, pages: Sequence[Any], cands: Iterable[Any], *, registry_hint: bool = False) -> None:
        """One attempt per people-bearing page type present (and per registry hint); produced = yielded someone."""
        try:
            produced = {candidate_key(c) for c in cands}
            tried = {page_key(_val(getattr(p, "page_type", None))) for p in pages}
            tried &= set(WEBSITE_PAGE_KEYS.values())
            tried |= {k for k in produced if k not in ("registry", "ai_extraction", "gemini_grounded_result")}
            if registry_hint:
                tried.add("registry")
            self.events += [S.StatEvent(DIMENSION, k, produced=k in produced) for k in sorted(tried)]
        except Exception as exc:  # pragma: no cover - defensive
            log.info("learning.people_attempts_failed", error=str(exc))

    def step(self, key: str, found: Sequence[Any], started: float) -> None:
        """An AI fallback step (``ai_extraction``, ``gemini_grounded_result``) that ran for this company.

        Not an attempt when AI is unavailable (the step returns [] without calling out)."""
        try:
            if not _ai_available():
                return
            cost = _grounded_cost_usd() if key == "gemini_grounded_result" else 0.0
            self.events.append(
                S.StatEvent(
                    DIMENSION,
                    key,
                    produced=bool(found),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    cost_usd=cost,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.info("learning.people_step_failed", error=str(exc))

    async def flush(self) -> None:
        events, self.events = self.events, []
        await S.record(events)


def _ai_available() -> bool:
    try:
        from scout.ai.factory import get_ai

        return bool(get_ai().available)
    except Exception:
        return False


def _grounded_cost_usd() -> float:
    """Configured cost of one grounded search (what usage accounting charges)."""
    try:
        from scout.config import get_settings

        return float(get_settings().cost_grounded_search_usd)
    except Exception:
        return 0.0
