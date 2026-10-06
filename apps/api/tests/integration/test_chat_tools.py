"""Chat operator tool execution (spec §32–§34): invalid arguments are rejected with a helpful, structured error
before any handler runs; tools are authorized server-side against the caller's workspace."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from scout.ai.provider import ToolCallRequest
from scout.auth.context import WorkspaceContext
from scout.chat.context import UIContext
from scout.chat.operator import execute_tool
from scout.chat.tools import ToolContext
from scout.db.engine import session_scope
from scout.db.enums import ActionStatus, EntityType, MemberRole
from scout.db.models import AssistantAction, List, ListMembership, Workspace
from scout.services import lists as lists_svc

pytestmark = pytest.mark.integration


def _ctx(ws: uuid.UUID, user: str, **ui) -> ToolContext:
    return ToolContext(
        ws=WorkspaceContext(workspace_id=ws, user_id=user, role=MemberRole.owner), ui=UIContext(**ui)
    )


async def _call(ctx: ToolContext, name: str, args: dict):
    return await execute_tool(ToolCallRequest(id="c1", name=name, args=args), ctx)


async def _action(action_id: uuid.UUID | None) -> AssistantAction:
    async with session_scope() as s:
        a = await s.get(AssistantAction, action_id)
        assert a is not None
        return a


async def test_invalid_arguments_return_a_helpful_error_and_nothing_runs(workspace):
    ws, user = workspace
    ctx = _ctx(ws, user)
    result, card, effects, status, confirm = await _call(ctx, "create_list", {"name": "", "colour": "red"})
    assert status == "failed" and effects == [] and confirm is None
    err = result["error"]
    assert err["code"] == "invalid_arguments"
    assert err["message"].startswith("Invalid arguments for create_list: ")
    assert "name: String should have at least 1 character" in err["message"]
    assert "colour: Extra inputs are not permitted" in err["message"]
    assert err["hint"] == "Accepted arguments: name, entity_type, description, rows"
    assert {tuple(d["loc"]) for d in err["details"]} == {("name",), ("colour",)}
    assert card is not None and card["kind"] == "error" and "name" in card["detail"]
    action = await _action(ctx.action_id)
    assert action.status == ActionStatus.failed and action.error == "invalid arguments"
    async with session_scope() as s:
        assert (
            await s.scalar(sa.select(sa.func.count()).select_from(List).where(List.workspace_id == ws)) == 0
        )


async def test_unknown_tool_is_refused(workspace):
    ws, user = workspace
    result, _card, _fx, status, _ = await _call(_ctx(ws, user), "drop_database", {})
    assert status == "failed" and result["error"]["code"] == "unknown_tool"


async def test_unknown_filter_field_lists_the_known_fields(workspace):
    ws, user = workspace
    result, card, _fx, status, _ = await _call(
        _ctx(ws, user),
        "filter_table",
        {"filter": {"conditions": [{"field": "favourite colour", "operator": "eq", "value": "blue"}]}},
    )
    assert status == "failed"
    err = result["error"]
    assert err["code"] == "validation_failed" and "favourite colour" in err["message"]
    assert "email_status" in err["hint"] and "icp_score" in err["hint"]
    assert card is not None and card["hint"] == err["hint"]


async def test_selection_target_without_selection_is_explained(workspace):
    ws, user = workspace
    result, _card, _fx, status, _ = await _call(
        _ctx(ws, user), "add_to_list", {"rows": {"target": "selection"}, "list_name": "Hot"}
    )
    assert status == "failed"
    assert result["error"]["message"] == "No rows are selected in the table"
    assert "select rows" in result["error"]["hint"].lower()


async def test_tools_cannot_touch_another_workspace(workspace):
    ws, user = workspace
    async with session_scope() as s:
        other = Workspace(name="Other", slug="other-" + uuid.uuid4().hex[:6])
        s.add(other)
        await s.flush()
        foreign, _ = await lists_svc.create_list(
            s, other.id, name="Their list", user_id=None, entity_type=EntityType.person
        )
        foreign_id = foreign.id
    ctx = _ctx(ws, user)
    result, _card, _fx, status, _ = await _call(
        ctx, "rename_list", {"list_id": str(foreign_id), "new_name": "Mine now"}
    )
    assert status == "failed" and result["error"]["code"] == "not_found"
    result, _card, _fx, status, _ = await _call(
        ctx,
        "add_to_list",
        {"rows": {"target": "ids", "ids": [str(uuid.uuid4())]}, "list_id": str(foreign_id)},
    )
    assert status == "failed" and result["error"]["code"] == "not_found"
    async with session_scope() as s:
        lst = await s.get(List, foreign_id)
        members = await s.scalar(
            sa.select(sa.func.count()).select_from(ListMembership).where(ListMembership.list_id == foreign_id)
        )
    assert lst is not None and lst.name == "Their list" and members == 0


async def test_valid_call_executes_and_is_audited(workspace):
    ws, user = workspace
    ctx = _ctx(ws, user)
    result, card, _fx, status, _ = await _call(ctx, "create_list", {"name": "Instagram Agencies"})
    assert status == "executed", result
    assert card is not None and card["action_id"] == str(ctx.action_id)
    action = await _action(ctx.action_id)
    assert action.status == ActionStatus.executed and action.tool_name == "create_list"
    async with session_scope() as s:
        assert await s.scalar(sa.select(List.name).where(List.workspace_id == ws)) == "Instagram Agencies"
