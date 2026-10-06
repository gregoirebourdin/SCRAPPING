"""Filter engine (spec §81): typed filter trees compiled to parameterized SQL over a field whitelist.

Never accepts raw SQL from clients or the AI: fields are looked up in a registry, operators are
validated against the field type, values are bound parameters.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scout.errors import ValidationFailed

Operator = Literal[
    "eq",
    "neq",
    "contains",
    "not_contains",
    "starts_with",
    "is_empty",
    "not_empty",
    "gt",
    "gte",
    "lt",
    "lte",
    "between",
    "in",
    "not_in",
    "before",
    "after",
    "is_true",
    "is_false",
    "is_unknown",
]

FieldType = Literal["text", "number", "enum", "date", "boolean", "percent"]


class FilterCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    operator: Operator
    value: Any = None


class FilterGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["and", "or"] = "and"
    conditions: list[FilterCondition | FilterGroup] = Field(default_factory=list)


class SortSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    direction: Literal["asc", "desc"] = "asc"


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    sql: str  # SQL expression over the base query aliases
    type: FieldType
    enum_values: tuple[str, ...] = ()
    sortable: bool = True
    filterable: bool = True
    custom_column_id: uuid.UUID | None = None


_OPS_BY_TYPE: dict[str, set[str]] = {
    "text": {"eq", "neq", "contains", "not_contains", "starts_with", "is_empty", "not_empty", "in", "not_in"},
    "enum": {"eq", "neq", "in", "not_in", "is_empty", "not_empty"},
    "number": {"eq", "neq", "gt", "gte", "lt", "lte", "between", "is_empty", "not_empty"},
    "percent": {"eq", "neq", "gt", "gte", "lt", "lte", "between", "is_empty", "not_empty"},
    "date": {"before", "after", "between", "is_empty", "not_empty", "gt", "lt"},
    "boolean": {"is_true", "is_false", "is_unknown", "eq", "neq", "is_empty", "not_empty"},
}


class Compiler:
    """Accumulates bind parameters while compiling expressions."""

    def __init__(self, fields: dict[str, FieldDef]) -> None:
        self.fields = fields
        self.params: dict[str, Any] = {}
        self._n = 0

    def bind(self, value: Any) -> str:
        name = f"f{self._n}"
        self._n += 1
        self.params[name] = value
        return f":{name}"

    def field(self, key: str) -> FieldDef:
        f = self.fields.get(key)
        if f is None:
            raise ValidationFailed(
                f"Unknown field '{key}'", hint="Use one of: " + ", ".join(sorted(self.fields)[:40])
            )
        return f

    # ----------------------------------------------------------------------------------- where
    def where(self, group: FilterGroup | None) -> str:
        if group is None or not group.conditions:
            return "TRUE"
        parts = []
        for c in group.conditions:
            parts.append(self.where(c) if isinstance(c, FilterGroup) else self.condition(c))
        joiner = " AND " if group.op == "and" else " OR "
        return "(" + joiner.join(parts) + ")"

    def condition(self, c: FilterCondition) -> str:
        f = self.field(c.field)
        if not f.filterable:
            raise ValidationFailed(f"Field '{c.field}' cannot be filtered")
        allowed = _OPS_BY_TYPE[f.type]
        if c.operator not in allowed:
            raise ValidationFailed(f"Operator '{c.operator}' is not valid for {f.type} field '{c.field}'")
        x = f"CAST({f.sql} AS numeric)" if f.type in ("number", "percent") else f.sql
        op = c.operator
        v = c.value
        if f.type == "boolean":
            # custom boolean columns store 'true'/'false'/'unknown' display values (UNKNOWN ≠ FALSE)
            if op == "is_true" or (op == "eq" and _truthy(v)):
                return f"({x} = 'true')"
            if op == "is_false" or (op == "eq" and v is not None and not _truthy(v)):
                return f"({x} = 'false')"
            if op == "is_unknown":
                return f"({x} IS NULL OR {x} NOT IN ('true','false'))"
            if op == "neq":
                return f"({x} IS DISTINCT FROM {self.bind('true' if _truthy(v) else 'false')})"
        if op == "is_empty":
            return f"({x} IS NULL OR CAST({x} AS text) = '')"
        if op == "not_empty":
            return f"({x} IS NOT NULL AND CAST({x} AS text) <> '')"
        if op in ("in", "not_in"):
            vals = v if isinstance(v, list) else [v]
            vals = [self._coerce(f, i) for i in vals if i is not None]
            if not vals:
                return "FALSE" if op == "in" else "TRUE"
            cast = "text" if f.type in ("text", "enum") else "numeric"
            if f.type in ("text",):
                expr = f"lower(CAST({x} AS text)) = ANY({self.bind([str(i).lower() for i in vals])})"
            else:
                expr = f"CAST({x} AS {cast}) = ANY({self.bind(vals)})"
            return f"({expr})" if op == "in" else f"(NOT ({expr}) OR {x} IS NULL)"
        if op == "between":
            if not isinstance(v, list | tuple) or len(v) != 2:
                raise ValidationFailed("between expects [min, max]")
            lo, hi = self._coerce(f, v[0]), self._coerce(f, v[1])
            return f"({x} BETWEEN {self.bind(lo)} AND {self.bind(hi)})"
        if op in ("contains", "not_contains", "starts_with"):
            needle = str(v or "").lower()
            pat = f"%{_escape_like(needle)}%" if op != "starts_with" else f"{_escape_like(needle)}%"
            expr = f"lower(CAST({x} AS text)) LIKE {self.bind(pat)} ESCAPE '\\'"
            return f"({expr})" if op != "not_contains" else f"(NOT ({expr}) OR {x} IS NULL)"
        val = self._coerce(f, v)
        sqlop = {
            "eq": "=",
            "neq": "IS DISTINCT FROM",
            "gt": ">",
            "gte": ">=",
            "lt": "<",
            "lte": "<=",
            "before": "<",
            "after": ">",
        }[op]
        if f.type == "text" and op in ("eq", "neq"):
            return f"(lower(CAST({x} AS text)) {sqlop} {self.bind(str(val).lower())})"
        return f"({x} {sqlop} {self.bind(val)})"

    def _coerce(self, f: FieldDef, v: Any) -> Any:
        if v is None:
            return None
        try:
            if f.type in ("number", "percent"):
                num = Decimal(str(v))
                if f.type == "percent" and num > 1:
                    num = num / Decimal(100)  # accept 80 for 0.8
                return num
            if f.type == "date":
                if isinstance(v, datetime | date):
                    return v
                return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if f.type == "enum":
                sv = str(v)
                if f.enum_values and sv not in f.enum_values:
                    # tolerate case differences (e.g. 'safe' → 'SAFE')
                    match = next(
                        (e for e in f.enum_values if e.lower() == sv.lower().replace("-", "_")), None
                    )
                    if match is None:
                        raise ValidationFailed(
                            f"Invalid value '{v}' for {f.key}; expected one of {list(f.enum_values)}"
                        )
                    return match
                return sv
        except (TypeError, ValueError) as exc:
            raise ValidationFailed(f"Invalid value '{v}' for {f.key}") from exc
        return v

    # ----------------------------------------------------------------------------------- order
    def order_keys(self, sort: list[SortSpec]) -> list[tuple[FieldDef, str]]:
        keys: list[tuple[FieldDef, str]] = []
        for sspec in sort[:3]:
            f = self.field(sspec.field)
            if not f.sortable:
                raise ValidationFailed(f"Field '{sspec.field}' cannot be sorted")
            keys.append((f, sspec.direction))
        return keys


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "oui", "y")


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


FilterGroup.model_rebuild()
