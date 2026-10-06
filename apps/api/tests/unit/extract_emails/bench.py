"""Email-extraction mini benchmark: precision / recall of the extractors on the labelled corpus.

    uv run python -m tests.unit.extract_emails.bench          # table + per-case errors

Extractors compared:
* ``legacy``       — the rules before the rework (``legacy.py``, frozen from git HEAD);
* ``current``      — ``scout.crawl.parser.parse_html(...).emails`` (``scout.extract.email_extract``);
* ``email_enrich`` — a Python port of waterdoog/email-enrich's HTML harvesting idea (MIT): global
  ``[at]`` / ``(at)`` / `` at `` / ``[dot]`` / `` dot `` / ``\\s*.\\s*`` rewriting of the raw HTML, a loose
  regex, ``mailto:`` and ``data-cfemail``; reimplemented here for evaluation only, not used in production.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from selectolax.lexbor import LexborHTMLParser

from scout.crawl.parser import parse_html
from scout.util.urls import registrable_domain
from tests.unit.extract_emails.corpus import CORPUS, Case
from tests.unit.extract_emails.legacy import legacy_extract

Extractor = Callable[[Case], set[str]]


def current_extract(case: Case) -> set[str]:
    return set(parse_html(case.html, case.url).emails)


def old_extract(case: Case) -> set[str]:
    return set(legacy_extract(case.html, case.url))


# ---- email-enrich port (ideas only; see module docstring) ---------------------------------------
_EE_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_EE_PLACEHOLDER_DOMAINS = {"example.com", "test.com", "sentry.io", "wixpress.com"}
_EE_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp")


def _ee_deobfuscate(s: str) -> str:
    s = re.sub(r"\[at\]|\(at\)|\sat\s", "@", s, flags=re.IGNORECASE)
    s = re.sub(r"\[dot\]|\(dot\)|\sdot\s", ".", s, flags=re.IGNORECASE)
    s = s.replace("&#64;", "@").replace("&#46;", ".")
    s = re.sub(r"\s*@\s*", "@", s)
    return re.sub(r"\s*\.\s*", ".", s)


def _ee_junk(email: str) -> bool:
    local, _, domain = email.partition("@")
    if not local or not domain or len(local) > 64 or len(email) > 254:
        return True
    if local.startswith(("noreply", "no-reply", "mailer-daemon")):
        return True
    return domain in _EE_PLACEHOLDER_DOMAINS or any(ext in email for ext in _EE_IMAGE_EXTS)


def _ee_cf(encoded: str) -> str:
    if not encoded or len(encoded) < 4 or len(encoded) % 2:
        return ""
    try:
        key = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i : i + 2], 16) ^ key) for i in range(2, len(encoded), 2))
    except ValueError:
        return ""


def email_enrich_extract(case: Case) -> set[str]:
    text = _ee_deobfuscate(case.html)
    out = {e.lower() for e in _EE_EMAIL.findall(_ee_deobfuscate(text)) if not _ee_junk(e.lower())}
    tree = LexborHTMLParser(text)
    for a in tree.css('a[href^="mailto:"]'):
        e = (
            re.sub(r"^mailto:", "", a.attributes.get("href") or "", flags=re.IGNORECASE)
            .split("?")[0]
            .strip()
            .lower()
        )
        if e and not _ee_junk(e):
            out.add(e)
    for node in tree.css("[data-cfemail]"):
        e = _ee_cf((node.attributes.get("data-cfemail") or "").strip())
        if e and not _ee_junk(e):
            out.add(e.lower())
    domain = registrable_domain(case.url) or ""
    return {e.replace(f"@www.{domain}", f"@{domain}") for e in out}


# ---- scoring ------------------------------------------------------------------------------------
@dataclass
class Score:
    name: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0


def score(name: str, extractor: Extractor, corpus: Iterable[Case] = CORPUS) -> Score:
    s = Score(name)
    for case in corpus:
        got = extractor(case)
        tp, fp, fn = got & case.expected, got - case.expected, case.expected - got
        s.tp, s.fp, s.fn = s.tp + len(tp), s.fp + len(fp), s.fn + len(fn)
        if fp or fn:
            s.errors.append(f"{case.id}: +{sorted(fp)} -{sorted(fn)}")
    return s


EXTRACTORS: dict[str, Extractor] = {
    "legacy": old_extract,
    "current": current_extract,
    "email_enrich": email_enrich_extract,
}


def run() -> dict[str, Score]:
    return {name: score(name, fn) for name, fn in EXTRACTORS.items()}


def main() -> None:
    cases = len(CORPUS)
    expected = sum(len(c.expected) for c in CORPUS)
    print(
        f"corpus: {cases} cases ({sum(c.kind == 'decoy' for c in CORPUS)} decoys), {expected} expected addresses"
    )
    print(f"{'extractor':<14}{'TP':>5}{'FP':>5}{'FN':>5}{'precision':>11}{'recall':>9}")
    results = run()
    for s in results.values():
        print(f"{s.name:<14}{s.tp:>5}{s.fp:>5}{s.fn:>5}{s.precision:>11.3f}{s.recall:>9.3f}")
    for s in results.values():
        print(f"\n[{s.name}] errors:")
        for err in s.errors:
            print("  ", err)


if __name__ == "__main__":
    main()
