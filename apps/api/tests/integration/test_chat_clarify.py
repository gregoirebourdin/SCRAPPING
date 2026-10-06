"""Chat operator: clarification protocol (≤ 3 questions, one round) → plan card → launch, visible steps,
"go" shortcuts, typed answers, deterministic fallback when the AI provider fails, and amend-with-confirmation —
all through `run_turn` with the deterministic local router (no API key)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa

from scout.ai.factory import set_ai
from scout.auth.context import WorkspaceContext
from scout.chat import operator
from scout.chat.clarify import ClarifyAnswer
from scout.chat.context import UIContext
from scout.db.engine import session_scope
from scout.db.enums import CampaignStatus, MemberRole
from scout.db.models import Campaign, ChatMessage, List
from scout.pipeline import campaigns as csvc
from scout.pipeline.icp import parse_prompt

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _local_ai():
    import scout.chat.local_router  # noqa: F401  (registers the deterministic router)

    set_ai(None)
    yield
    set_ai(None)


async def _say(
    ws: uuid.UUID, user: str, thread_id: uuid.UUID | None, text: str, **kw: Any
) -> tuple[uuid.UUID, list[tuple[str, dict[str, Any]]]]:
    wctx = WorkspaceContext(workspace_id=ws, user_id=user, role=MemberRole.owner)
    thread = await operator.get_or_create_thread(wctx, thread_id, None)
    events = [ev async for ev in operator.run_turn(wctx, thread, text, UIContext(), **kw)]
    return thread.id, events


def _cards(events, kind: str) -> list[dict[str, Any]]:
    return [d["card"] for e, d in events if e == "tool_result" and d.get("card") and d["card"]["kind"] == kind]


async def _campaigns(ws: uuid.UUID) -> list[Campaign]:
    async with session_scope() as s:
        return list((await s.scalars(sa.select(Campaign).where(Campaign.workspace_id == ws))).all())


async def test_vague_request_asks_questions_then_plans_then_launches_once(workspace):
    ws, user = workspace
    thread, events = await _say(ws, user, None, "Trouve des agences marketing")
    # visible steps: understanding → preparing questions, each completed with a duration
    steps = [d for e, d in events if e == "step"]
    assert steps[0] == {"id": "understand", "label": "Je comprends ta demande…", "status": "active"}
    done = {d["id"]: d for d in steps if d["status"] == "done"}
    assert "understand" in done and any(d["label"] == "Je prépare quelques questions…" for d in done.values())
    assert all("ms" in d for d in done.values())
    texts = "".join(d["delta"] for e, d in events if e == "text")
    assert texts == "3 précisions rapides avant de lancer la recherche :"
    (card,) = _cards(events, "clarify")
    assert [q["id"] for q in card["questions"]] == ["geography", "roles", "volume"]
    assert all(len(q["options"]) <= 4 for q in card["questions"])
    assert card["lang"] == "fr" and card["action_id"]
    assert await _campaigns(ws) == [], "questions never start anything"

    # structured answers → plan card ("Voici ce que je vais chercher") — still nothing launched
    _, events = await _say(
        ws,
        user,
        thread,
        "Lyon · Fondateurs / dirigeants · 50",
        clarification=operator.ClarificationIn(
            action_id=uuid.UUID(card["action_id"]),
            answers=[
                ClarifyAnswer(id="geography", value="Lyon", label="Lyon"),
                ClarifyAnswer(id="roles", value="founders", label="Fondateurs / dirigeants"),
                ClarifyAnswer(id="volume", value="50", label="50"),
            ],
        ),
    )
    assert not [d for e, d in events if e == "error"]
    (plan,) = _cards(events, "campaign_plan")
    interp = {i["label"]: i["value"] for i in plan["interpretation"]}
    assert interp["Location"] == "Lyon, France" and interp["Target"].startswith("50")
    assert plan["title"] == "Voici ce que je vais chercher"
    assert any(e == "card_update" and d["patch"]["answered"] for e, d in events)
    assert await _campaigns(ws) == []
    async with session_scope() as s:
        msgs = (
            await s.scalars(
                sa.select(ChatMessage).where(ChatMessage.thread_id == thread).order_by(ChatMessage.created_at)
            )
        ).all()
    first_cards = [p["card"] for p in msgs[1].parts if p.get("type") == "card"]
    assert first_cards[0]["answered"] is True, "the answered state survives a reload"
    assert msgs[-1].parts[0]["type"] == "steps", "completed steps are persisted with the message"

    # Launch (twice: double click / second tab) → exactly one campaign
    wctx = WorkspaceContext(workspace_id=ws, user_id=user, role=MemberRole.owner)
    first = await operator.launch_plan(wctx, uuid.UUID(plan["action_id"]), UIContext())
    second = await operator.launch_plan(wctx, uuid.UUID(plan["action_id"]), UIContext())
    assert first["campaign_id"] == second["campaign_id"] and second["already"] is True
    (camp,) = await _campaigns(ws)
    assert camp.status == CampaignStatus.planning and camp.target_qualified_count == 50
    assert camp.definition["company_filters"]["cities"] == ["Lyon"]
    assert first["card"]["kind"] == "campaign_started"
    assert first["ui_effects"] == [{"type": "open_list", "list_id": str(camp.target_list_id)}]


async def test_precise_request_goes_straight_to_the_plan(workspace):
    ws, user = workspace
    _, events = await _say(
        ws, user, None, "Find 40 marketing agencies in Lyon with 2-30 employees, founders, safe email"
    )
    assert not _cards(events, "clarify")
    (plan,) = _cards(events, "campaign_plan")
    assert plan["target"] == 40 and plan["lang"] == "en"
    assert await _campaigns(ws) == []


async def test_go_word_skips_questions_and_launches(workspace):
    ws, user = workspace
    _, events = await _say(ws, user, None, "Trouve 30 agences marketing à Lyon, vas-y")
    assert not _cards(events, "clarify") and not _cards(events, "campaign_plan")
    (started,) = _cards(events, "campaign_started")
    (camp,) = await _campaigns(ws)
    assert started["campaign_id"] == str(camp.id) and camp.target_qualified_count == 30


async def test_typed_answer_then_go_launches_the_pending_plan(workspace):
    ws, user = workspace
    thread, events = await _say(ws, user, None, "Trouve des agences marketing")
    assert _cards(events, "clarify")
    # free-text answer under the question card
    _, events = await _say(ws, user, thread, "à Marseille, 25 leads, les fondateurs")
    (plan,) = _cards(events, "campaign_plan")
    assert {i["label"]: i["value"] for i in plan["interpretation"]}["Location"] == "Marseille, France"
    assert await _campaigns(ws) == []
    # "vas-y" under the plan card launches it
    _, events = await _say(ws, user, thread, "vas-y")
    (started,) = _cards(events, "campaign_started")
    (camp,) = await _campaigns(ws)
    assert started["campaign_id"] == str(camp.id)
    assert camp.definition["company_filters"]["cities"] == ["Marseille"]
    assert any(e == "ui_effect" and d["type"] == "open_list" for e, d in events)


async def test_use_defaults_and_answer_and_launch(workspace):
    ws, user = workspace
    thread, events = await _say(ws, user, None, "Find dentists in Lyon")
    (card,) = _cards(events, "clarify")
    _, events = await _say(
        ws,
        user,
        thread,
        "Use defaults",
        clarification=operator.ClarificationIn(action_id=uuid.UUID(card["action_id"]), use_defaults=True, launch=True),
    )
    (plan,) = _cards(events, "campaign_plan")
    (started,) = _cards(events, "campaign_started")
    assert plan["target"] == 50  # the default of the volume question
    (camp,) = await _campaigns(ws)
    assert started["campaign_id"] == str(camp.id)


async def test_a_different_command_under_a_question_card_is_not_taken_as_an_answer(workspace):
    ws, user = workspace
    thread, events = await _say(ws, user, None, "Trouve des agences marketing")
    assert _cards(events, "clarify")
    _, events = await _say(ws, user, thread, "Create a list called Hot Leads")
    assert _cards(events, "list_created")
    async with session_scope() as s:
        assert await s.scalar(sa.select(List.id).where(List.workspace_id == ws, List.name == "Hot Leads"))


class _BrokenProvider:
    name = "gemini"

    @property
    def available(self) -> bool:
        return True

    async def chat_stream(self, **_: Any) -> AsyncIterator[Any]:
        raise RuntimeError("Gemini 503: model overloaded")
        yield  # pragma: no cover

    async def structured(self, **_: Any) -> Any:
        from scout.errors import AIUnavailable

        raise AIUnavailable("down")


async def test_provider_failure_falls_back_to_the_deterministic_router(workspace):
    ws, user = workspace
    set_ai(_BrokenProvider())  # type: ignore[arg-type]
    _, events = await _say(ws, user, None, "Create a list called Fallback List")
    assert not [d for e, d in events if e == "error"]
    assert any(e == "step" and d["id"] == "fallback" for e, d in events)
    assert _cards(events, "list_created")


async def test_resume_with_a_change_shows_a_diff_then_applies_and_resumes(workspace):
    ws, user = workspace
    defn, _ = await parse_prompt("Find 20 marketing agencies in Lyon, founders")
    async with session_scope() as s:
        c = await csvc.create_campaign(s, ws, defn, user_id=user, prompt="agencies")
        cid = c.id
    async with session_scope() as s:
        await csvc.pause_campaign(s, ws, cid)
    thread, events = await _say(ws, user, None, "reprends en ajoutant Marseille")
    (confirm,) = [d for e, d in events if e == "confirm"]
    assert confirm["danger"] is False and confirm["confirm_label"] == "Apply & resume"
    assert confirm["changes"] == [
        {
            "field": "location",
            "label": "Location",
            "before": "Lyon, France",
            "after": "Lyon, Marseille, France",
            "safe": False,
        }
    ]
    async with session_scope() as s:
        assert (await s.get(Campaign, cid)).status == CampaignStatus.paused, "nothing changes before confirming"
    wctx = WorkspaceContext(workspace_id=ws, user_id=user, role=MemberRole.owner)
    out = await operator.confirm_action(wctx, uuid.UUID(confirm["action_id"]), UIContext(), approve=True)
    assert out["status"] == "executed" and out["card"]["kind"] == "campaign_amended"
    assert out["card"]["resumed"] is True
    async with session_scope() as s:
        c2 = await s.get(Campaign, cid)
        assert c2.status == CampaignStatus.running
        assert c2.definition["company_filters"]["cities"] == ["Lyon", "Marseille"]
        msg = (
            await s.scalars(
                sa.select(ChatMessage)
                .where(ChatMessage.thread_id == thread, ChatMessage.role == "assistant")
                .order_by(ChatMessage.created_at.desc())
            )
        ).first()
    parts = msg.parts
    assert any(p.get("type") == "confirm" and p.get("status") == "approved" for p in parts)
    assert any(p.get("type") == "card" and p["card"]["kind"] == "campaign_amended" for p in parts)
    # a second confirmation (double click) is refused, nothing runs twice
    from scout.errors import AppError

    with pytest.raises(AppError):
        await operator.confirm_action(wctx, uuid.UUID(confirm["action_id"]), UIContext(), approve=True)


async def test_pause_and_resume_from_the_chat(workspace):
    ws, user = workspace
    defn, _ = await parse_prompt("Find 20 marketing agencies in Lyon, founders")
    async with session_scope() as s:
        c = await csvc.create_campaign(s, ws, defn, user_id=user, prompt="agencies")
        cid = c.id
    thread, events = await _say(ws, user, None, "arrête la recherche")
    (card,) = _cards(events, "campaign_progress")
    assert card["event"] == "Paused"
    assert "conservé" in "".join(d["delta"] for e, d in events if e == "text")
    _, events = await _say(ws, user, thread, "reprends")
    assert _cards(events, "campaign_progress")[0]["event"] == "Resumed"
    async with session_scope() as s:
        assert (await s.get(Campaign, cid)).status == CampaignStatus.running
