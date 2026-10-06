"""People stage (processor ``_stage_people``): registry names and website picks first, with no fallback call;
otherwise AI extraction ∥ free web search (best candidates win), then Gemini grounding as last resort — all
within ``PEOPLE_FALLBACK_BUDGET_S``. Offline: database helpers and every fallback are replaced by stubs."""

from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from scout.ai.factory import FakeProvider, set_ai
from scout.config import Settings, get_settings
from scout.db.models import Company
from scout.discovery.fr_registry import map_result
from scout.extract.types import PersonCandidate
from scout.learning.people import PeopleLearning
from scout.pipeline import processor
from scout.schemas.campaign import CampaignDefinition
from scout.search import reset_state
from scout.search.chain import HEALTH
from tests.unit.extract.helpers import agence_pages, page_from_html

REGISTRY_PAGE = Path(__file__).resolve().parents[2] / "fixtures" / "discovery" / "fr_registry_page.json"
COMPANIES: dict[uuid.UUID, Company] = {}

NO_PEOPLE_HOME = page_from_html(
    "<html><body><h1>Atelier Nord</h1><p>Agence web et marketing digital à Bordeaux. Sites, SEO et "
    "campagnes pour les PME de la région.</p></body></html>",
    "https://atelier-nord.fr/",
)


def cand(name: str, title: str, source_type: str, confidence: float) -> PersonCandidate:
    first, _, last = name.partition(" ")
    return PersonCandidate(
        full_name=name,
        first_name=first,
        last_name=last,
        title=title,
        source_url="https://atelier-nord.fr/equipe",
        source_type=source_type,
        method={"ai_extraction": "ai", "search_snippet": "search_result", "grounded_search": "grounded"}[
            source_type
        ],
        evidence=f"{name}, {title}",
        confidence=confidence,
    )


AI_PICK = cand("Claire Fontaine", "Fondatrice", "ai_extraction", 0.8)
SERP_PICK = cand("Paul Martin", "CEO", "search_snippet", 0.65)
GROUNDED_PICK = cand("Sophie Bernard", "Gérante", "grounded_search", 0.7)


class Steps:
    """Stand-ins for the three fallbacks: each sleeps ``delay`` then returns its candidates; calls, starts
    and cancellations are recorded."""

    def __init__(self) -> None:
        self.plan: dict[str, tuple[float, list[PersonCandidate]]] = {
            "ai": (0.0, []),
            "serp": (0.0, []),
            "grounded": (0.0, []),
        }
        self.started: dict[str, float] = {}
        self.cancelled: set[str] = set()
        self.serp_kwargs: dict[str, Any] = {}

    async def _run(self, name: str) -> list[PersonCandidate]:
        self.started[name] = time.monotonic()
        delay, found = self.plan[name]
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled.add(name)
            raise
        return [copy.copy(c) for c in found]

    async def ai(self, pages: Any, *, company_name: str) -> list[PersonCandidate]:
        return await self._run("ai")

    async def serp(self, company_name: str, **kw: Any) -> list[PersonCandidate]:
        self.serp_kwargs = kw
        return await self._run("serp")

    async def grounded(self, ctx: Any, comp: Any) -> list[PersonCandidate]:
        return await self._run("grounded")


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Settings]]:
    def apply(**env: str | None) -> Settings:
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        return get_settings()

    get_settings.cache_clear()
    yield apply
    get_settings.cache_clear()
    reset_state()


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch, settings_env: Callable[..., Settings]) -> Iterator[Steps]:
    settings_env(
        SEARXNG_URL="http://searxng.test:8080",
        SEARCH_LOOKUP_PROVIDERS="searxng",
        GEMINI_SEARCH_FALLBACK_ONLY="true",
        PEOPLE_FALLBACK_BUDGET_S="2",
    )
    reset_state()
    st = Steps()
    monkeypatch.setattr("scout.extract.ai_people.ai_extract_people", st.ai)
    monkeypatch.setattr("scout.search.people.serp_people", st.serp)
    monkeypatch.setattr(processor, "_grounded_people", st.grounded)
    set_ai(FakeProvider())
    yield st
    set_ai(None)


@pytest.fixture
def stage(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """``_stage_people`` without a database: company, stage/counter writes and learning I/O stubbed.
    Returns the learning events the stage recorded."""
    events: list[Any] = []

    async def start(cls: type[PeopleLearning]) -> PeopleLearning:
        return cls({}, use=False)

    async def flush(self: PeopleLearning) -> None:
        events.extend(self.events)
        self.events = []

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(PeopleLearning, "start", classmethod(start))
    monkeypatch.setattr(PeopleLearning, "flush", flush)
    monkeypatch.setattr(processor, "_count_once", noop)
    monkeypatch.setattr(processor, "_set_stage", noop)
    return events


def make_ctx(pages: list[Any], name: str = "Atelier Nord", domain: str = "atelier-nord.fr") -> processor.Ctx:
    company_id = uuid.uuid4()
    COMPANIES[company_id] = Company(name=name, normalized_domain=domain, country="FR", city="Bordeaux")
    return processor.Ctx(
        workspace_id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        company_id=company_id,
        defn=CampaignDefinition.model_validate(
            {
                "company_filters": {"industries": ["digital marketing agency"], "cities": ["Bordeaux"]},
                "people_filters": {"titles": ["Founder", "CEO"]},
            }
        ),
        target_list_id=None,
        source_key="fr_registry",
        pages=pages,
    )


@pytest.fixture(autouse=True)
def _company(monkeypatch: pytest.MonkeyPatch) -> None:
    async def company(ctx: processor.Ctx) -> Company:
        return COMPANIES[ctx.company_id]

    monkeypatch.setattr(processor, "_company", company)


def registry_results() -> list[dict[str, Any]]:
    return json.loads(REGISTRY_PAGE.read_text(encoding="utf-8"))["results"]


def names(picks: list[processor.PersonPick]) -> list[tuple[str, str]]:
    return [(p.candidate.full_name, p.candidate.source_type) for p in picks]


# ---- registry and website first: no fallback call ------------------------------------------------------


async def test_website_picks_make_no_fallback_call(steps: Steps, stage: list[Any]) -> None:
    picks = await processor._stage_people(make_ctx(agence_pages(), "Agence Lumière", "agence-lumiere.fr"), {})
    assert picks and all(p.candidate.source_type == "website" for p in picks)
    assert steps.started == {}


async def test_registry_directors_come_before_any_fallback(steps: Steps, stage: list[Any]) -> None:
    item = next(r for r in registry_results() if r["siren"] == "839985603")
    hints = {"people": map_result(item).people, "registry_source": "fr_sirene"}
    picks = await processor._stage_people(make_ctx([NO_PEOPLE_HOME]), hints)
    assert names(picks) == [("Pierre Calmard", "registry")]  # "Président de SAS" matches founder / CEO
    assert steps.started == {}
    assert ("people.source", "registry", True) in [(e.dimension, e.key, e.produced) for e in stage]


@pytest.mark.parametrize("owner", ["JEAN DUPONT", "MARIE BOIS"])  # "Bois" is a word, still a surname
async def test_sole_proprietor_owner_is_the_registry_pick(steps: Steps, stage: list[Any], owner: str) -> None:
    item = registry_results()[0]
    item.update(
        {"nature_juridique": "1000", "nom_complet": owner, "nom_raison_sociale": None, "dirigeants": []}
    )
    item["siege"].update({"nom_commercial": None, "liste_enseignes": None})
    raw = map_result(item)
    assert raw is not None and raw.name == owner  # the company is named after its owner
    picks = await processor._stage_people(
        make_ctx([NO_PEOPLE_HOME], raw.name, "jean-dupont.fr"),
        {"people": raw.people, "registry_source": "fr_sirene"},
    )
    assert names(picks) == [(owner.title(), "registry")]
    pick = picks[0]
    assert pick.title_info is not None and pick.title_score >= 0.9
    assert (pick.candidate.first_name, pick.candidate.last_name) == tuple(owner.title().split())
    assert steps.started == {}


async def test_registry_hint_with_non_matching_role_still_falls_back(steps: Steps, stage: list[Any]) -> None:
    steps.plan["ai"] = (0.0, [AI_PICK])
    hints = {"people": [{"full_name": "Luc Morel", "title": "Directeur Général Délégué"}]}
    picks = await processor._stage_people(make_ctx([NO_PEOPLE_HOME]), hints)
    assert names(picks) == [("Claire Fontaine", "ai_extraction")]


# ---- fallbacks: concurrency, budget, grounded as last resort --------------------------------------------


async def test_ai_and_search_run_concurrently_and_the_best_wins(steps: Steps, stage: list[Any]) -> None:
    steps.plan["ai"] = (0.3, [AI_PICK])
    steps.plan["serp"] = (0.3, [SERP_PICK])
    t0 = time.monotonic()
    picks = await processor._stage_people(make_ctx([NO_PEOPLE_HOME]), {})
    elapsed = time.monotonic() - t0
    assert names(picks) == [("Claire Fontaine", "ai_extraction"), ("Paul Martin", "search_snippet")]
    assert elapsed < 0.55  # in parallel, not 0.6 s back to back
    assert abs(steps.started["ai"] - steps.started["serp"]) < 0.1
    assert "grounded" not in steps.started
    assert steps.serp_kwargs["domain"] == "atelier-nord.fr" and steps.serp_kwargs["titles"] == [
        "Founder",
        "CEO",
    ]
    assert [(e.key, e.produced) for e in stage if e.key == "ai_extraction"] == [("ai_extraction", True)]


async def test_search_alone_is_enough(steps: Steps, stage: list[Any]) -> None:
    steps.plan["serp"] = (0.05, [SERP_PICK])
    picks = await processor._stage_people(make_ctx([NO_PEOPLE_HOME]), {})
    assert names(picks) == [("Paul Martin", "search_snippet")]
    assert "grounded" not in steps.started


async def test_slow_search_gets_a_short_grace_once_ai_found_someone(
    steps: Steps, stage: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processor, "SEARCH_GRACE_S", 0.1)
    steps.plan["ai"] = (0.05, [AI_PICK])
    steps.plan["serp"] = (5.0, [SERP_PICK])
    t0 = time.monotonic()
    picks = await processor._stage_people(make_ctx([NO_PEOPLE_HOME]), {})
    assert time.monotonic() - t0 < 0.5
    assert names(picks) == [("Claire Fontaine", "ai_extraction")]
    assert "serp" in steps.cancelled


async def test_grounded_is_the_last_resort_within_the_remaining_budget(
    steps: Steps, stage: list[Any]
) -> None:
    steps.plan["ai"] = (0.05, [])
    steps.plan["serp"] = (0.05, [])
    steps.plan["grounded"] = (0.05, [GROUNDED_PICK])
    picks = await processor._stage_people(make_ctx([NO_PEOPLE_HOME]), {})
    assert names(picks) == [("Sophie Bernard", "grounded_search")]
    assert steps.started["grounded"] >= max(steps.started["ai"], steps.started["serp"]) + 0.05
    assert ("gemini_grounded_result", True) in [(e.key, e.produced) for e in stage]


async def test_nothing_runs_past_the_budget(
    steps: Steps, stage: list[Any], settings_env: Callable[..., Settings]
) -> None:
    settings_env(PEOPLE_FALLBACK_BUDGET_S="0.5")
    steps.plan = {"ai": (10.0, [AI_PICK]), "serp": (10.0, [SERP_PICK]), "grounded": (10.0, [GROUNDED_PICK])}
    t0 = time.monotonic()
    with pytest.raises(processor.Rejection) as rej:
        await processor._stage_people(make_ctx([NO_PEOPLE_HOME]), {})
    elapsed = time.monotonic() - t0
    assert rej.value.reason == "No decision maker found" and rej.value.stage == "people"
    assert 0.45 <= elapsed < 0.8
    assert steps.cancelled == {"ai", "serp", "grounded"}
    # the free steps leave the grounded lookup its share of the budget (40 % here), it never runs past it
    assert steps.started["grounded"] - steps.started["ai"] == pytest.approx(0.3, abs=0.1)
    # a step cut by the budget is still a recorded (unproductive) attempt
    assert {(e.key, e.produced) for e in stage if e.key != "registry"} >= {
        ("ai_extraction", False),
        ("gemini_grounded_result", False),
    }


async def test_search_skipped_while_the_lookup_chain_cools_down(steps: Steps, stage: list[Any]) -> None:
    HEALTH.failure("searxng", "searxng: no results, engines failing", blocked=True)
    steps.plan["grounded"] = (0.0, [GROUNDED_PICK])
    picks = await processor._stage_people(make_ctx([NO_PEOPLE_HOME]), {})
    assert "serp" not in steps.started and "ai" in steps.started
    assert names(picks) == [("Sophie Bernard", "grounded_search")]


async def test_no_grounding_near_the_budget_limit_and_free_steps_get_the_whole_budget(
    steps: Steps, stage: list[Any], monkeypatch: pytest.MonkeyPatch, settings_env: Callable[..., Settings]
) -> None:
    async def deny() -> bool:
        return False

    monkeypatch.setattr(processor, "allow_expensive", deny)
    settings_env(PEOPLE_FALLBACK_BUDGET_S="0.5")
    steps.plan["ai"] = (0.4, [AI_PICK])  # beyond 60 % of the budget, inside the budget
    picks = await processor._stage_people(make_ctx([NO_PEOPLE_HOME]), {})
    assert names(picks) == [("Claire Fontaine", "ai_extraction")]
    assert "grounded" not in steps.started


async def test_ai_failure_is_an_unproductive_attempt(
    steps: Steps, stage: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(pages: Any, *, company_name: str) -> list[PersonCandidate]:
        raise RuntimeError("Gemini 503")

    monkeypatch.setattr("scout.extract.ai_people.ai_extract_people", boom)
    steps.plan["serp"] = (0.0, [SERP_PICK])
    picks = await processor._stage_people(make_ctx([NO_PEOPLE_HOME]), {})
    assert names(picks) == [("Paul Martin", "search_snippet")]
    assert ("ai_extraction", False) in [(e.key, e.produced) for e in stage]
