"""Where learned stats drive decisions: people keys/confidence, feedback events, planner, discovery router."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from scout.db.enums import ColumnDataType, SourceType
from scout.extract.types import PersonCandidate
from scout.learning import feedback, routing
from scout.learning import stats as S
from scout.learning.people import PeopleLearning, people_source_key
from scout.learning.stats import StatRow


def row(attempts=0, successes=0, correct=0, wrong=0) -> StatRow:
    return StatRow(
        attempts=attempts,
        correct=correct,
        wrong=wrong,
        inconclusive=0,
        latency_ms_total=0,
        successes=successes,
    )


# ---- people.source keys --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kwargs", "key"),
    [
        ({"source_type": "website", "page_type": "team"}, "official_team_page"),
        ({"source_type": "website", "source_url": "https://acme.fr/equipe"}, "official_team_page"),
        ({"source_type": "website", "source_url": "https://acme.fr/mentions-legales"}, "legal_notice"),
        ({"source_type": "website", "source_url": "https://acme.fr/a-propos"}, "about_page"),
        ({"source_type": "website", "source_url": "https://acme.fr/"}, "home_page"),
        ({"source_type": "website", "page_type": "other"}, "website_other"),
        ({"source_type": "website", "page_type": "about", "method": "legal_notice"}, "legal_notice"),
        ({"source_type": SourceType.registry}, "registry"),
        ({"source_type": "ai_extraction", "page_type": "team"}, "ai_extraction"),
        ({"source_type": "grounded_search"}, "gemini_grounded_result"),
        ({"source_type": "search_snippet", "source_url": "https://fr.linkedin.com/in/x"}, "linkedin_snippet"),
        ({"source_type": "search_snippet", "source_url": "https://example.org/x"}, "searxng_result"),
        ({"source_type": "directory"}, "directory"),
        ({"source_type": SourceType.user}, "user"),
    ],
)
def test_people_source_key(kwargs, key) -> None:
    st = kwargs.pop("source_type")
    assert people_source_key(st, **kwargs) == key


def _cand(**kw) -> PersonCandidate:
    base = dict(
        full_name="Marie Dupont",
        first_name="Marie",
        last_name="Dupont",
        title="CEO",
        source_url="https://acme.fr/equipe",
        source_type="website",
        method="team_card",
        evidence="Marie Dupont — CEO",
        confidence=0.88,
        page_type="team",
    )
    base.update(kw)
    return PersonCandidate(**base)


def test_people_learning_adjusts_confidence_and_collects_attempts() -> None:
    snap = {("people.source", "official_team_page"): row(attempts=200, correct=20, wrong=80)}
    team, legal = (
        _cand(),
        _cand(full_name="Paul Martin", method="legal_notice", page_type="legal", confidence=0.9),
    )
    learn = PeopleLearning(snap)
    learn.adjust([team, legal])
    assert team.confidence < 0.88 and legal.confidence == 0.9  # only the source with evidence moves

    off = PeopleLearning(snap, use=False)
    c = _cand()
    off.adjust([c])
    assert c.confidence == 0.88  # setting off ⇒ hard-coded constants

    pages = [
        SimpleNamespace(page_type="team"),
        SimpleNamespace(page_type="about"),
        SimpleNamespace(page_type="blog"),
    ]
    learn.website(pages, [team], registry_hint=True)
    got = {(e.key, e.produced) for e in learn.events}
    assert got == {("official_team_page", True), ("about_page", False), ("registry", False)}


# ---- feedback events ---------------------------------------------------------------------------------
@dataclass
class Cell:
    value_json: object
    status: str = "success"
    is_user_override: bool = False


def test_cell_override_events() -> None:
    ev = feedback.cell_override_events("keyword", Cell(True), False)
    assert [(e.dimension, e.key, e.correct, e.outcome) for e in ev] == [
        ("enrich.resolver", "keyword", False, True),
        ("enrich.resolver", "user", True, True),
    ]
    assert [e.correct for e in feedback.cell_override_events("ai_extraction", Cell(" Paris "), "paris")] == [
        True
    ]
    assert feedback.cell_override_events("keyword", Cell(True, is_user_override=True), False) == []
    assert feedback.cell_override_events("keyword", Cell(None, status="unknown"), True) == []
    assert feedback.cell_override_events("generated_text", Cell("x"), "y") == []
    assert feedback.cell_override_events("keyword", None, True) == []


def _obs(value, url="https://acme.fr/equipe", st="website", user=False):
    return SimpleNamespace(value_json=value, source_type=st, source_url=url, is_user_confirmed=user)


def test_person_edit_events() -> None:
    obs = [
        _obs("CTO"),  # team page said CTO (current value)
        _obs("CEO", url="https://acme.fr/mentions-legales"),  # legal notice already had the right title
        _obs("CTO", st="grounded_search", url=None),
    ]
    ev = feedback.person_edit_events(obs, "CTO", "CEO")
    assert {(e.key, e.correct) for e in ev} == {
        ("official_team_page", False),
        ("gemini_grounded_result", False),
        ("legal_notice", True),
        ("user", True),
    }
    assert feedback.person_edit_events([*obs, _obs("CEO", user=True)], "CEO", "Founder") == []  # user-owned
    same = feedback.person_edit_events(obs, "CTO", "cto")  # re-typed the same value: a confirmation
    assert {(e.key, e.correct) for e in same} == {
        ("official_team_page", True),
        ("gemini_grounded_result", True),
    }


# ---- planner -----------------------------------------------------------------------------------------
@pytest.fixture
def no_ai():
    from scout.ai.factory import LocalProvider, set_ai

    set_ai(LocalProvider())
    yield
    set_ai(None)


async def test_planner_unchanged_without_evidence_and_switches_with_it(no_ai, monkeypatch) -> None:
    from scout.enrich.planner import fallback_order, learned_reliability_hint, plan_column

    plan = await plan_column("Eco", "Eco-friendly packaging", data_type=ColumnDataType.boolean)
    assert plan.strategy == "semantic_classifier"
    assert fallback_order(["semantic_classifier", "keyword"], {}) == ["semantic_classifier", "keyword"]
    assert learned_reliability_hint({}) == ""

    # learned: the AI classifier rarely answers here (no AI) while keyword matching is reliable
    snap = {
        ("enrich.resolver", "semantic_classifier"): row(attempts=300, successes=6, correct=4, wrong=8),
        ("enrich.resolver", "keyword"): row(attempts=300, successes=290, correct=60, wrong=2),
    }
    assert fallback_order(["semantic_classifier", "keyword"], snap) == ["keyword", "semantic_classifier"]
    assert "keyword" in learned_reliability_hint(snap)
    monkeypatch.setattr(S, "_snapshot", snap)
    learned = await plan_column("Eco", "Eco-friendly packaging", data_type=ColumnDataType.boolean)
    assert learned.strategy == "keyword" and learned.keywords and "learned reliability" in learned.explanation
    # strong deterministic rules are never overridden
    assert (await plan_column("Shopify", "Add whether they use Shopify")).strategy == "tech_detection"
    # kill switch
    monkeypatch.setattr(routing, "enabled", lambda: False)
    assert (
        await plan_column("Eco", "Eco-friendly packaging", data_type=ColumnDataType.boolean)
    ).strategy == ("semantic_classifier")


# ---- discovery router ----------------------------------------------------------------------------------
def test_router_learned_factor(monkeypatch) -> None:
    from scout.ai.factory import FakeProvider, set_ai
    from scout.discovery.router import learned_factors, select_sources
    from tests.unit.discovery.conftest import defn

    set_ai(FakeProvider())
    try:
        d = defn(industries=["agences marketing"], countries=["FR"], employee_range=(2, 30))
        base = select_sources(d, learned={})
        assert learned_factors(["fr_registry"], d, {}) == {}
        top = base[0][0].key
        # the top source turns out to deliver almost no qualified leads, another one excels
        other = next(s.key for s, _ in base[1:])
        snap = {
            ("discovery.source", top): row(attempts=400, successes=40, correct=2, wrong=150),
            ("discovery.source", other): row(attempts=400, successes=390, correct=150, wrong=10),
        }
        f = learned_factors([top, other], d, snap)
        assert f[top] == routing.FACTOR_RANGE[0] or f[top] < 1.0
        learned = select_sources(d, learned=snap)
        assert learned[0][0].key == other
        assert select_sources(d, learned=snap) == learned  # deterministic per campaign definition
        # implicit snapshot (warmed by health_snapshot) and kill switch
        monkeypatch.setattr(S, "_snapshot", snap)
        assert select_sources(d)[0][0].key == other
        monkeypatch.setattr(routing, "enabled", lambda: False)
        assert [s.key for s, _ in select_sources(d)] == [s.key for s, _ in base]
    finally:
        set_ai(None)
