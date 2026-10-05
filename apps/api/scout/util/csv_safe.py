"""CSV injection protection (OWASP): neutralize formula-leading cells."""

from __future__ import annotations

from typing import Any

_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value)
    if s and s.startswith(_DANGEROUS_PREFIXES):
        # numbers like -12.5 are safe to keep as numbers
        if s[0] in "+-":
            try:
                float(s)
                return s
            except ValueError:
                pass
        return "'" + s
    return s
