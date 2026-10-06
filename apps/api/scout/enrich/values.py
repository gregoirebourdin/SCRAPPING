"""Cell value coercion and display formatting (deterministic)."""

from __future__ import annotations

import re
from typing import Any

import orjson

from scout.db.enums import ColumnDataType
from scout.enrich.matching import fold
from scout.errors import ValidationFailed

_TRUE = {"true", "yes", "y", "oui", "vrai", "1", "x", "✓"}
_FALSE = {"false", "no", "n", "non", "faux", "0", "-", "–"}
_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_NUMBER = re.compile(r"-?\d[\d\s  .,]*")
_URLISH = re.compile(r"^(https?://)?([a-z0-9-]+\.)+[a-z]{2,}(/\S*)?$", re.I)
MAX_TEXT = 2000


def display_for(value: Any, data_type: ColumnDataType | str | None = None) -> str | None:
    """Text used for sorting/filtering: "true"/"false" for booleans, joined lists, trimmed text."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:g}"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v is not None)[:MAX_TEXT]
    if isinstance(value, dict):
        return orjson.dumps(value).decode()[:MAX_TEXT]
    return str(value).strip()[:MAX_TEXT]


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    key = fold(str(value)).strip().strip(".!")
    if key in _TRUE:
        return True
    if key in _FALSE:
        return False
    return None


def parse_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if value is None:
        return None
    m = _NUMBER.search(str(value))
    if not m:
        return None
    raw = re.sub(r"[\s  ]", "", m.group(0))
    if raw.count(",") == 1 and raw.count(".") == 0:
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    if raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def coerce_value(value: Any, data_type: ColumnDataType | str, enum_values: list[str] | None = None) -> Any:
    """Coerce a raw value to the column type. Returns None when it cannot be represented faithfully."""
    dt = ColumnDataType(str(getattr(data_type, "value", data_type)))
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or fold(value) in {"unknown", "none", "null", "n/a", "na", "inconnu", "not found"}:
            return None
    if dt == ColumnDataType.boolean:
        return parse_bool(value)
    if dt == ColumnDataType.number:
        num = parse_number(value)
        if num is None:
            return None
        return int(num) if num.is_integer() else num
    if dt == ColumnDataType.email:
        s = str(value).strip().lower().removeprefix("mailto:")
        return s if _EMAIL.match(s) else None
    if dt == ColumnDataType.url:
        s = str(value).strip()
        if not _URLISH.match(s):
            return None
        return s if s.lower().startswith(("http://", "https://")) else f"https://{s}"
    if dt == ColumnDataType.enum:
        allowed = enum_values or []
        if not allowed:
            return str(value)[:200]
        key = fold(str(value)).strip()
        for opt in allowed:
            if fold(opt).strip() == key:
                return opt
        return None
    if dt == ColumnDataType.json:
        return value
    if dt == ColumnDataType.date:
        return str(value)[:40]
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)[:MAX_TEXT]
    return str(value)[:MAX_TEXT]


def coerce_user_value(
    value: Any, data_type: ColumnDataType | str, enum_values: list[str] | None = None
) -> Any:
    """Coerce a user-entered value; raise ValidationFailed when it does not fit the column type."""
    out = coerce_value(value, data_type, enum_values)
    if out is None:
        raise ValidationFailed(
            f"Value does not match the column type ({getattr(data_type, 'value', data_type)})"
        )
    return out
