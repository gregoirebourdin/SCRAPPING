"""AI tool argument contracts (spec §32–§34, §199 "AI tool arguments"): strict Pydantic schemas reject
malformed / out-of-range / unknown arguments before any handler runs, and every schema exports to a
function declaration."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

import scout.chat.tools  # noqa: F401  (registers the tools)
from scout.chat.operator import tool_specs
from scout.chat.tools import TOOLS


def _validate(tool: str, args: dict):
    return TOOLS[tool].args.model_validate(args)


@pytest.mark.parametrize(
    ("tool", "args", "loc"),
    [
        ("create_list", {"name": ""}, "name"),
        ("create_list", {"name": "x" * 121}, "name"),
        ("create_list", {"name": "Ok", "entity_type": "robot"}, "entity_type"),
        ("create_list", {"name": "Ok", "colour": "red"}, "colour"),  # unknown argument
        ("add_to_list", {"list_name": "A"}, "rows"),  # rows are required
        ("add_to_list", {"rows": {"target": "everything"}}, "rows.target"),
        ("add_to_list", {"rows": {"target": "ids"}}, "rows"),  # ids target without ids
        ("add_to_list", {"rows": {"target": "ids", "ids": ["not-a-uuid"]}}, "rows.ids.0"),
        ("add_to_list", {"rows": {"target": "filter"}}, "rows"),  # filter target without conditions
        (
            "filter_table",
            {"filter": {"conditions": [{"field": "email_status", "operator": "LIKE", "value": "SAFE"}]}},
            "filter.conditions.0.operator",
        ),
        (
            "filter_table",
            {"filter": {"conditions": [{"field": "", "operator": "eq", "value": "x"}]}},
            "filter.conditions.0.field",
        ),
        ("filter_table", {"filter": {"match": "most", "conditions": []}}, "filter.match"),
        ("create_column", {"name": "Offers Instagram"}, "instruction"),
        ("create_column", {"name": "", "instruction": "x"}, "name"),
        ("create_column", {"name": "A", "instruction": "x", "scope": "galaxy"}, "scope"),
        ("refresh_column", {"column_name": "A", "older_than_days": 0}, "older_than_days"),
        ("find_more_leads", {"count": 0}, "count"),
        ("find_more_leads", {"count": 1_000_000}, "count"),
        ("find_decision_makers", {"titles": []}, "titles"),
        ("find_decision_makers", {"titles": ["CMO"], "max_per_company": 50}, "max_per_company"),
        ("create_campaign", {"request": ""}, "request"),
        ("exclude_previous_leads", {"mode": "EXCLUDE_EVERYONE"}, "mode"),
        ("exclude_previous_leads", {"mode": "EXCLUDE_WITHIN_COOLDOWN", "cooldown_days": -3}, "cooldown_days"),
        ("verify_emails", {"statuses": ["MAYBE"]}, "statuses.0"),
        ("export_leads", {"format": "xlsx"}, "format"),
        ("update_cell", {"entity_id": str(uuid.uuid4()), "field": "job_title"}, "value"),
        ("suppress_leads", {"rows": {"target": "selection"}, "reason": "because"}, "reason"),
    ],
)
def test_invalid_arguments_are_rejected(tool, args, loc):
    assert tool in TOOLS, f"unknown tool {tool}"
    with pytest.raises(ValidationError) as exc:
        _validate(tool, args)
    locs = {".".join(str(x) for x in e["loc"]) for e in exc.value.errors()}
    assert any(lc == loc or lc.startswith(loc + ".") or lc.startswith(loc) for lc in locs), locs


def test_rows_errors_explain_what_is_missing():
    with pytest.raises(ValidationError, match="target 'ids' requires a non-empty `ids` list"):
        _validate("add_to_list", {"rows": {"target": "ids", "ids": []}})
    with pytest.raises(
        ValidationError, match="target 'filter' requires `filter` with at least one condition"
    ):
        _validate("add_to_list", {"rows": {"target": "filter", "filter": {"conditions": []}}})


def test_valid_arguments_are_accepted():
    a = _validate(
        "add_to_list",
        {
            "rows": {
                "target": "filter",
                "filter": {"conditions": [{"field": "email_status", "operator": "eq", "value": "SAFE"}]},
            },
            "list_name": "Instagram Agencies",
        },
    )
    assert a.rows.filter.conditions[0].value == "SAFE"
    assert _validate("create_list", {"name": "Instagram Agencies"}).name == "Instagram Agencies"
    assert _validate("find_more_leads", {"count": 100}).count == 100


def test_every_tool_exports_a_function_declaration():
    specs = {s.name: s for s in tool_specs()}
    assert set(specs) == set(TOOLS)
    for spec in specs.values():
        assert spec.parameters.get("type") == "object"
        assert spec.description
    assert specs["create_list"].parameters["required"] == ["name"]
