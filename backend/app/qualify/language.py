"""Language detection (langdetect, deterministic seed) combined with the HTML ``lang`` attribute."""

from __future__ import annotations

import re

from langdetect import DetectorFactory, LangDetectException, detect_langs

DetectorFactory.seed = 0

_STRIP = re.compile(r"(https?://\S+|\S+@\S+|[\d$€£%]+|[^\w\s'’.,!?-])")


def detect_language(text: str, lang_attr: str = "") -> tuple[str, float]:
    """Return (iso-639-1 code, confidence 0..1)."""
    sample = _STRIP.sub(" ", text or "")[:8000]
    sample = re.sub(r"\s+", " ", sample).strip()
    attr = (lang_attr or "").lower().replace("_", "-").split("-")[0]
    if len(sample) < 120:
        return (attr or "und", 0.6 if attr else 0.0)
    try:
        langs = detect_langs(sample)
    except LangDetectException:
        return (attr or "und", 0.5 if attr else 0.0)
    if not langs:
        return (attr or "und", 0.5 if attr else 0.0)
    top = langs[0]
    code, prob = top.lang, float(top.prob)
    if attr and attr == code:
        prob = min(1.0, prob + 0.1)
    elif attr == "en" and code != "en" and prob < 0.9:
        # detector unsure and the site declares English (common with short, keyword-heavy marketing pages)
        return ("en", 0.78)
    return (code, prob)


def is_english(code: str, confidence: float, threshold: float) -> bool:
    return code == "en" and confidence >= threshold
