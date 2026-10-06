"""Address normalization and syntax validation (no DNS, no network)."""

from __future__ import annotations

import idna
from email_validator import EmailNotValidError, validate_email

MAX_ADDRESS_LEN = 254


def normalize_domain(domain: str | None) -> str | None:
    """Lowercase, strip dots/whitespace and IDNA-encode a domain; None when unusable."""
    if not domain:
        return None
    d = domain.strip().strip(".").lower()
    if not d or " " in d or "@" in d or "/" in d:
        return None
    try:
        d = idna.encode(d, uts46=True).decode("ascii")
    except idna.IDNAError:
        return None
    return d if "." in d else None


def normalize_address(s: str | None) -> str | None:
    """Trim, strip a `mailto:` prefix, lowercase and IDNA-encode the domain. None if not `x@y`."""
    if not s:
        return None
    a = s.strip().strip("<>").strip()
    if a.lower().startswith("mailto:"):
        a = a[7:].split("?", 1)[0].strip()
    if a.count("@") != 1:
        return None
    local, _, domain = a.rpartition("@")
    local = local.strip().lower()
    dom = normalize_domain(domain)
    if not local or dom is None:
        return None
    addr = f"{local}@{dom}"
    return addr if len(addr) <= MAX_ADDRESS_LEN else None


def split_address(addr: str) -> tuple[str, str]:
    """('local', 'domain') of an already normalized address."""
    local, _, domain = addr.rpartition("@")
    return local, domain


def is_valid_syntax(addr: str | None) -> bool:
    """RFC-compliant syntax check (email-validator, deliverability check disabled)."""
    if not addr:
        return False
    try:
        validate_email(addr, check_deliverability=False)
    except EmailNotValidError:
        return False
    return True
