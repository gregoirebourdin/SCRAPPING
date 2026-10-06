"""User feedback → ground-truth outcomes (docs/ARCHITECTURE.md §Empirical Source Scoring).

* A user overrides an enrichment cell produced by strategy X: different value → X confirmed wrong (and
  ``user`` confirmed correct); same value → X confirmed correct.
* A user edits a person field: every source whose observation held the replaced value → wrong; every source
  whose observation already held the new value → correct.
* A user approves a person (review queue): sources whose observations hold the approved name/title → correct.
* Discovery: a candidate rejected at company qualification → the discovery source was wrong (off-ICP); a
  qualified lead → correct (``scout.discovery.health.record_outcomes``).

Cheap (one indexed query at most) and idempotent-ish: once a value is user-owned (user override / user
observation) further edits do not count against the original source again. Recorded inside the caller's
transaction under a savepoint, so a stats failure never breaks the edit.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import orjson
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from scout.learning import stats as S
from scout.learning.people import DIMENSION as PEOPLE_DIMENSION
from scout.learning.people import people_source_key

log = structlog.get_logger("learning.feedback")

ENRICH_DIMENSION = "enrich.resolver"
DISCOVERY_DIMENSION = "discovery.source"
NON_FACTUAL_STRATEGIES = frozenset({"generated_text"})
PERSON_FEEDBACK_FIELDS = frozenset({"full_name", "job_title", "public_profile_url", "location", "phone"})


def normalize_value(v: Any) -> Any:
    """Comparable form of a cell / field value (case, whitespace, list order and number type insensitive)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return round(float(v), 6)
    if isinstance(v, str):
        return " ".join(v.split()).casefold()
    if isinstance(v, list | tuple):
        return tuple(sorted((normalize_value(x) for x in v), key=repr))
    if isinstance(v, dict):
        return orjson.dumps({k: normalize_value(x) for k, x in v.items()}, option=orjson.OPT_SORT_KEYS)
    return str(v)


def same_value(a: Any, b: Any) -> bool:
    return normalize_value(a) == normalize_value(b)


def outcome(dimension: str, key: str | None, correct: bool | None) -> list[S.StatEvent]:
    return [S.StatEvent(dimension, key, correct=correct, outcome=True)] if key else []


# ---------------------------------------------------------------------------------------------
# Enrichment cells
# ---------------------------------------------------------------------------------------------
def cell_override_events(strategy: str | None, previous: Any, new_value: Any) -> list[S.StatEvent]:
    """Outcomes for a user override of a cell (``previous``: the CustomFieldValue before the edit)."""
    if previous is None or new_value is None or not strategy or strategy in NON_FACTUAL_STRATEGIES:
        return []
    if getattr(previous, "is_user_override", False):
        return []  # already user-owned: never count the original resolver twice
    status = str(getattr(getattr(previous, "status", None), "value", getattr(previous, "status", "")))
    if status != "success" or getattr(previous, "value_json", None) is None:
        return []  # nothing was asserted (unknown / failed): not a precision event
    if same_value(previous.value_json, new_value):
        return outcome(ENRICH_DIMENSION, strategy, True)
    return outcome(ENRICH_DIMENSION, strategy, False) + outcome(ENRICH_DIMENSION, "user", True)


async def cell_overridden(s: AsyncSession, strategy: str | None, previous: Any, new_value: Any) -> None:
    try:
        await S.record_in(s, cell_override_events(strategy, previous, new_value))
    except Exception as exc:  # pragma: no cover - record_in never raises; belt and braces
        log.info("learning.cell_feedback_failed", error=str(exc))


# ---------------------------------------------------------------------------------------------
# Person fields
# ---------------------------------------------------------------------------------------------
def _obs_key(o: Any) -> str:
    return people_source_key(o.source_type, source_url=o.source_url)


def person_edit_events(observations: Sequence[Any], current: Any, new_value: Any) -> list[S.StatEvent]:
    """Outcomes for a user edit of one person field, from that field's observations before the edit."""
    if any(getattr(o, "is_user_confirmed", False) for o in observations):
        return []  # the current value is already user-owned
    events: list[S.StatEvent] = []
    changed = current is not None and not same_value(current, new_value)
    wrong: set[str] = set()
    right: set[str] = set()
    for o in observations:
        if changed and same_value(o.value_json, current):
            wrong.add(_obs_key(o))
        elif same_value(o.value_json, new_value):
            right.add(_obs_key(o))
    for k in sorted(wrong):
        events += outcome(PEOPLE_DIMENSION, k, False)
    for k in sorted(right - wrong):
        events += outcome(PEOPLE_DIMENSION, k, True)
    if changed:
        events += outcome(PEOPLE_DIMENSION, "user", True)
    return events


async def _observations(s: AsyncSession, person_ids: Sequence[uuid.UUID], fields: Sequence[str]) -> list[Any]:
    from scout.db.models import PersonFieldObservation as O

    return list(
        (
            await s.scalars(
                sa.select(O).where(O.person_id.in_(list(person_ids)), O.field_name.in_(list(fields)))
            )
        ).all()
    )


async def person_field_overridden(s: AsyncSession, person: Any, field: str, new_value: Any) -> None:
    """Call before the user observation is added (the person still holds the previous value)."""
    if field not in PERSON_FEEDBACK_FIELDS:
        return
    try:
        obs = await _observations(s, [person.id], [field])
        await S.record_in(s, person_edit_events(obs, getattr(person, field, None), new_value))
    except Exception as exc:
        log.info("learning.person_feedback_failed", error=str(exc))


async def people_approved(s: AsyncSession, people: Sequence[Any]) -> None:
    """Call before the approval observations are added. Sources holding the approved values → correct."""
    if not people:
        return
    try:
        fields = ("full_name", "job_title")
        obs = await _observations(s, [p.id for p in people], fields)
        by: dict[tuple[uuid.UUID, str], list[Any]] = {}
        for o in obs:
            by.setdefault((o.person_id, o.field_name), []).append(o)
        events: list[S.StatEvent] = []
        for p in people:
            for f in fields:
                group = by.get((p.id, f), [])
                current = getattr(p, f, None)
                if current is None or any(o.is_user_confirmed for o in group):
                    continue
                for k in sorted({_obs_key(o) for o in group if same_value(o.value_json, current)}):
                    events += outcome(PEOPLE_DIMENSION, k, True)
        await S.record_in(s, events)
    except Exception as exc:
        log.info("learning.approval_feedback_failed", error=str(exc))


# ---------------------------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------------------------
async def discovery_outcome(source_key: str | None, *, correct: int = 0, wrong: int = 0) -> None:
    """Qualified leads (correct) / off-ICP rejections (wrong) for a discovery source. Never raises."""
    if not source_key:
        return
    events = [S.StatEvent(DISCOVERY_DIMENSION, source_key, correct=True, outcome=True)] * max(0, correct)
    events += [S.StatEvent(DISCOVERY_DIMENSION, source_key, correct=False, outcome=True)] * max(0, wrong)
    await S.record(events)
