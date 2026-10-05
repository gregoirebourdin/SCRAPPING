"""Text normalization helpers (deterministic, no AI)."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from unidecode import unidecode

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Legal-form suffixes / prefixes removed from company names before comparison.
LEGAL_FORMS = {
    # FR
    "sas", "sasu", "sarl", "eurl", "sa", "sci", "snc", "scop", "sca", "selarl", "ei", "eirl", "micro entreprise",
    "auto entrepreneur", "ste", "societe", "société",
    # EN
    "ltd", "limited", "llc", "inc", "incorporated", "corp", "corporation", "co", "company", "plc", "llp", "lp",
    # DE/AT/CH
    "gmbh", "ag", "ug", "kg", "ohg", "gbr", "mbh", "e k", "ek",
    # ES/IT/PT/NL/BE
    "sl", "slu", "srl", "spa", "sas di", "lda", "bv", "nv", "bvba", "sprl", "vof",
    # Nordics etc.
    "ab", "as", "aps", "oy", "oyj",
}

GENERIC_COMPANY_WORDS = {"the", "group", "groupe", "agency", "agence", "studio", "and", "et", "&"}


def collapse_ws(s: str) -> str:
    return _WS.sub(" ", s).strip()


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def ascii_fold(s: str) -> str:
    return unidecode(s)


def normalize_key(s: str) -> str:
    """Lowercase, accent-free, punctuation collapsed to single spaces."""
    s = ascii_fold(s).lower()
    s = _NON_ALNUM.sub(" ", s)
    return collapse_ws(s)


def normalize_company_name(name: str) -> str:
    """Normalized legal/company name used for dedupe (legal forms removed)."""
    key = normalize_key(name)
    tokens = key.split()
    # remove legal forms at the end/start (multi-pass for "xxx sas france")
    joined = " ".join(tokens)
    for form in sorted(LEGAL_FORMS, key=len, reverse=True):
        joined = re.sub(rf"(^| ){re.escape(form)}($| )", " ", joined)
    joined = collapse_ws(joined)
    return joined or key


def normalize_person_name(name: str) -> str:
    """lowercase, unaccented, collapsed whitespace; hyphens kept as spaces."""
    return normalize_key(name)


def sha256_hex(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8", "ignore")
    return hashlib.sha256(s).hexdigest()


def content_hash(text: str) -> str:
    """Stable hash of normalized page text (whitespace/case insensitive)."""
    return sha256_hex(collapse_ws(text).lower())


def truncate(s: str | None, n: int) -> str | None:
    if s is None:
        return None
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def slugify(s: str, max_len: int = 60) -> str:
    slug = normalize_key(s).replace(" ", "_")
    return slug[:max_len].strip("_") or "column"


def title_case_name(s: str) -> str:
    """'JEAN-PIERRE DUPONT' → 'Jean-Pierre Dupont' (keeps particles lowercase)."""
    particles = {"de", "du", "des", "la", "le", "van", "von", "der", "den", "di", "da", "del", "d"}
    out = []
    for i, word in enumerate(collapse_ws(s).split(" ")):
        parts = word.split("-")
        cased = []
        for p in parts:
            if not p:
                continue
            low = p.lower()
            if i > 0 and low in particles:
                cased.append(low)
            elif "'" in p:
                a, b = p.split("'", 1)
                cased.append(a.capitalize() + "'" + b.capitalize())
            else:
                cased.append(p[:1].upper() + p[1:].lower())
        out.append("-".join(cased))
    return " ".join(out)
