"""Exclusion modes compile into explicit rules (PIPELINE.md §5). Evaluation in SQL is covered by
tests/integration/test_registry_exclusion.py."""

from __future__ import annotations

import uuid

import pytest

from scout.db.enums import EntityType, ExclusionMode, ExposureType
from scout.schemas.campaign import ExclusionSpec
from scout.services.exclusion import compile_rules, default_exclusion_for_prompt

P, C = EntityType.person, EntityType.company


def _rules(**kw):
    return [
        (r.entity, r.exposure_types, r.within_days, r.list_ids, r.import_ids)
        for r in compile_rules(ExclusionSpec(**kw))
    ]


def test_none_compiles_nothing():
    assert compile_rules(ExclusionSpec()) == []


def test_previous_people_allows_new_people_at_known_companies():
    assert _rules(mode=ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE) == [(P, None, None, [], [])]
    both = _rules(mode=ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE, allow_new_people_at_existing_companies=False)
    assert both == [(P, None, None, [], []), (C, None, None, [], [])]


def test_previous_companies_and_both():
    assert _rules(mode=ExclusionMode.EXCLUDE_PREVIOUS_COMPANIES) == [(C, None, None, [], [])]
    assert _rules(mode=ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE_AND_COMPANIES) == [
        (P, None, None, [], []),
        (C, None, None, [], []),
    ]


@pytest.mark.parametrize(
    ("mode", "exposure"),
    [
        (ExclusionMode.EXCLUDE_EXPORTED, ExposureType.EXPORTED),
        (ExclusionMode.EXCLUDE_CONTACTED, ExposureType.CONTACTED),
    ],
)
def test_exposure_specific_modes(mode, exposure):
    assert _rules(mode=mode) == [(P, [exposure], None, [], [])]
    assert _rules(mode=mode, previous_companies=True) == [
        (P, [exposure], None, [], []),
        (C, [exposure], None, [], []),
    ]


def test_cooldown():
    assert _rules(mode=ExclusionMode.EXCLUDE_WITHIN_COOLDOWN, cooldown_days=30) == [(P, None, 30, [], [])]
    assert _rules(mode=ExclusionMode.EXCLUDE_WITHIN_COOLDOWN) == [(P, None, 90, [], [])]


def test_specific_and_current_lists():
    a, b, target = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert _rules(mode=ExclusionMode.EXCLUDE_SPECIFIC_LISTS, list_ids=[a, b]) == [(P, None, None, [a, b], [])]
    assert _rules(mode=ExclusionMode.EXCLUDE_SPECIFIC_LISTS, list_ids=[a], list_scope="both") == [
        (P, None, None, [a], []),
        (C, None, None, [a], []),
    ]
    current = compile_rules(ExclusionSpec(mode=ExclusionMode.EXCLUDE_CURRENT_LIST), target_list_id=target)
    assert [(r.entity, r.list_ids) for r in current] == [(P, [target])]
    assert all(r.include_list_history for r in current)  # removed members stay excluded (spec §203)


def test_import_exclusion_applies_in_any_mode():
    imp = uuid.uuid4()
    assert (P, None, None, [], [imp]) in _rules(mode=ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE, import_ids=[imp])
    assert _rules(import_ids=[imp]) == [(P, None, None, [], [imp])]


def test_custom_rules_are_explicit():
    lst = uuid.uuid4()
    spec = ExclusionSpec(
        mode=ExclusionMode.CUSTOM,
        rules=[
            {"entity": "company", "exposure_types": ["EXPORTED"], "within_days": 7},
            {"entity": "person", "list_ids": [str(lst)], "include_list_history": False},
        ],
    )
    rules = compile_rules(spec)
    assert [
        (r.entity, r.exposure_types, r.within_days, r.list_ids, r.include_list_history) for r in rules
    ] == [(C, [ExposureType.EXPORTED], 7, [], True), (P, None, None, [lst], False)]


def test_template_flags_complete_mode_none():
    assert _rules(previous_people=True) == [(P, None, None, [], [])]
    assert _rules(previous_people=True, previous_companies=True) == [
        (P, None, None, [], []),
        (C, None, None, [], []),
    ]


@pytest.mark.parametrize(
    "prompt",
    [
        "Find 100 French marketing agencies. Do not include leads already seen.",
        "only new leads please",
        "Trouve des agences jamais vues",
        "leads I've never scraped",
    ],
)
def test_default_exclusion_for_fresh_requests(prompt):
    assert default_exclusion_for_prompt(prompt) == ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE


def test_no_default_exclusion_otherwise():
    assert default_exclusion_for_prompt("Find marketing agencies in Lyon") is None
