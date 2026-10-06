"""AI-assisted people extraction for ambiguous team/about pages (≈10 % AI path).

Hallucination guard: every returned name AND its evidence quote must appear verbatim (accent-,
case- and whitespace-insensitive) in the page the model was shown, and the name must pass the
deterministic person-name checks. Anything else is dropped. Returns [] when AI is unavailable.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence

import structlog
from pydantic import BaseModel, Field

from scout.ai.factory import get_ai
from scout.ai.models import ModelRole
from scout.ai.prompts import EXTRACTION_SYSTEM, wrap_untrusted
from scout.crawl.types import PageLike
from scout.db.enums import PageType, SourceType
from scout.extract.names import display_name, is_plausible_person_name, split_name
from scout.extract.titles import is_job_title
from scout.extract.types import PersonCandidate
from scout.util.text import ascii_fold

log = structlog.get_logger(__name__)

MAX_CONFIDENCE = 0.8
MAX_CHARS_PER_PAGE = 12_000
_PAGE_PRIORITY = {PageType.team.value: 0, PageType.about.value: 1, PageType.home.value: 2}


class AIPerson(BaseModel):
    full_name: str = Field(description="Person's full name exactly as written on the page")
    title: str | None = Field(default=None, description="Job title exactly as written on the page, or null")
    evidence_quote: str = Field(description="Verbatim excerpt of the page that names this person (and title)")
    source_url: str = Field(description="URL of the page the person was found on")


class AIPeopleExtraction(BaseModel):
    people: list[AIPerson] = Field(default_factory=list)


_PROMPT = """Extract the people who WORK AT the company "{company}" (founders, executives, managers, staff)
from the website page below.

Rules:
- Only people explicitly named on the page as members of this company's team.
- Exclude client testimonials, reviewers, quoted customers, partners, blog authors without a role,
  and people from other companies.
- full_name, title and evidence_quote must be copied verbatim from the page. Do not translate or complete names.
- If nobody qualifies, return an empty list. Never guess.

source_url for every person: {url}

{content}"""


def _fold(s: str) -> str:
    s = ascii_fold(s or "").lower()
    s = re.sub(r"[’`´]", "'", s)
    s = re.sub(r"[‐-―−]", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def appears_verbatim(needle: str, haystack: str) -> bool:
    """Accent/case/whitespace-insensitive containment (also tolerant to ellipses in quotes)."""
    n = _fold(needle).strip(" .,;:\"'«»“”")
    if len(n) < 3:
        return False
    h = _fold(haystack)
    if n in h:
        return True
    parts = [p.strip() for p in re.split(r"\s*(?:\.\.\.|…)\s*", n) if p.strip()]
    return len(parts) > 1 and all(len(p) >= 3 and p in h for p in parts)


def _ptype(page: PageLike) -> str:
    pt = getattr(page, "page_type", None)
    return str(pt.value if isinstance(pt, PageType) else pt or "other")


async def ai_extract_people(
    pages: Sequence[PageLike], *, company_name: str, max_pages: int = 3
) -> list[PersonCandidate]:
    """AI extraction over team/about/home pages with strict verbatim post-validation."""
    ai = get_ai()
    if not ai.available:
        return []
    selected = sorted(
        (p for p in pages if _ptype(p) in _PAGE_PRIORITY and len((p.content_text or "").strip()) >= 80),
        key=lambda p: _PAGE_PRIORITY[_ptype(p)],
    )[:max_pages]

    async def ask(page: PageLike) -> list[AIPerson]:
        prompt = _PROMPT.format(
            company=company_name.replace('"', "'"),
            url=page.url,
            content=wrap_untrusted((page.content_text or "")[:MAX_CHARS_PER_PAGE], source=page.url),
        )
        try:
            result = await ai.structured(
                role=ModelRole.extractor, system=EXTRACTION_SYSTEM, prompt=prompt, schema=AIPeopleExtraction
            )
        except Exception as exc:  # AI failures never break the deterministic pipeline
            log.info("ai_people_failed", url=page.url, error=str(exc)[:200])
            return []
        return result.value.people

    # One call per page, all at once (bounded by the gemini pool); validated in page priority order.
    answers = await asyncio.gather(*(ask(p) for p in selected))
    out: list[PersonCandidate] = []
    seen: set[str] = set()
    for page, people in zip(selected, answers, strict=True):
        content = (page.content_text or "")[:MAX_CHARS_PER_PAGE]
        for person in people:
            name = re.sub(r"\s+", " ", person.full_name or "").strip()
            quote = (person.evidence_quote or "").strip()
            if not name or not quote:
                continue
            if not is_plausible_person_name(name):
                log.info("ai_people_rejected", reason="implausible_name", name=name)
                continue
            if not appears_verbatim(name, content) or not appears_verbatim(quote, content):
                log.info("ai_people_rejected", reason="not_verbatim", name=name, url=page.url)
                continue
            title = (person.title or "").strip() or None
            if title and not appears_verbatim(title, content):
                title = None  # never keep an invented title
            key = _fold(name)
            if key in seen:
                continue
            seen.add(key)
            confidence = (
                MAX_CONFIDENCE if (title and is_job_title(title) and appears_verbatim(name, quote)) else 0.7
            )
            shown = display_name(name)
            first, last = split_name(shown)
            out.append(
                PersonCandidate(
                    full_name=shown,
                    first_name=first,
                    last_name=last,
                    title=title,
                    source_url=page.url,
                    source_type=SourceType.ai_extraction.value,
                    method="ai",
                    evidence=quote[:500],
                    confidence=confidence,
                    page_type=_ptype(page),
                )
            )
    return out
