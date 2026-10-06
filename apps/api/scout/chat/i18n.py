"""Tiny FR/EN helpers for the chat operator: language detection and the short, human step sentences shown
(with a shimmer) while the assistant works. Steps describe what the app is doing — never hidden reasoning."""

from __future__ import annotations

import re
from typing import Any, Literal

Lang = Literal["fr", "en"]

_FR_WORDS = re.compile(
    r"\b(je|tu|nous|vous|les|des|une?|du|dans|avec|pour|sans|seulement|aussi|trouve\w*|cherche\w*|donne\w*|"
    r"entreprises?|agences?|soci[ée]t[ée]s?|dirigeants?|fondateurs?|reprend\w*|ajoute\w*|arr[eê]te\w*|"
    r"lance\w*|vas[- ]y|oui|non|merci|plut[oô]t|salari[ée]s?|employ[ée]s|personnes)\b",
    re.I,
)
_EN_WORDS = re.compile(
    r"\b(the|find|get|with|without|only|also|companies|agencies|people|please|and|for|resume|add|leads?)\b",
    re.I,
)


def detect_lang(text: str) -> Lang:
    """French when the message reads French (accents or common words outnumber English ones)."""
    t = text or ""
    fr = len(_FR_WORDS.findall(t)) + 2 * len(re.findall(r"[éèêàùçôî]", t, re.I))
    en = len(_EN_WORDS.findall(t))
    return "fr" if fr > en else "en"


SOURCE_LABELS: dict[str, dict[Lang, str]] = {
    "google_maps": {"fr": "Google Maps", "en": "Google Maps"},
    "fr_registry": {"fr": "annuaire des entreprises", "en": "French company registry"},
    "osm": {"fr": "OpenStreetMap", "en": "OpenStreetMap"},
    "web_search": {"fr": "recherche web", "en": "web search"},
    "gemini_search": {"fr": "recherche IA", "en": "AI web research"},
    "yc": {"fr": "annuaire YC", "en": "YC directory"},
    "hn_hiring": {"fr": "offres HN", "en": "HN hiring threads"},
    "github": {"fr": "GitHub", "en": "GitHub"},
    "fixture": {"fr": "annuaire de démo", "en": "demo directory"},
    "seed": {"fr": "ta liste", "en": "your list"},
}


def source_label(key: str, lang: Lang) -> str:
    return SOURCE_LABELS.get(key, {}).get(lang, key.replace("_", " "))


_T: dict[str, dict[Lang, str]] = {
    "understand": {"fr": "Je comprends ta demande…", "en": "Understanding your request…"},
    "understood": {"fr": "Demande comprise", "en": "Request understood"},
    "clarify": {"fr": "Je prépare quelques questions…", "en": "Preparing a few questions…"},
    "plan": {"fr": "Je prépare le plan de recherche…", "en": "Preparing the search plan…"},
    "launch": {"fr": "Je lance la recherche…", "en": "Starting the search…"},
    "pause": {"fr": "Je mets la recherche en pause…", "en": "Pausing the search…"},
    "resume": {"fr": "Je reprends la recherche…", "en": "Resuming the search…"},
    "cancel": {"fr": "J'arrête la recherche…", "en": "Stopping the search…"},
    "amend": {"fr": "J'applique ta modification…", "en": "Applying your change…"},
    "status": {"fr": "Je regarde où en est la recherche…", "en": "Checking the search progress…"},
    "filter": {"fr": "Je filtre le tableau…", "en": "Filtering the table…"},
    "sort": {"fr": "Je trie le tableau…", "en": "Sorting the table…"},
    "column": {"fr": "Je prépare la colonne…", "en": "Setting up the column…"},
    "enrich": {"fr": "J'enrichis les lignes…", "en": "Enriching rows…"},
    "emails": {"fr": "Recherche des emails…", "en": "Finding emails…"},
    "verify": {"fr": "Vérification des emails…", "en": "Verifying emails…"},
    "lists": {"fr": "Je mets à jour tes listes…", "en": "Updating your lists…"},
    "export": {"fr": "Je prépare l'export…", "en": "Preparing the export…"},
    "read": {"fr": "Je consulte tes données…", "en": "Looking at your data…"},
    "generic": {"fr": "J'exécute l'action…", "en": "Running the action…"},
    "fallback": {
        "fr": "L'IA ne répond pas — je passe en mode déterministe",
        "en": "The AI provider is not responding — switching to deterministic mode",
    },
    "answers": {"fr": "Je prends en compte tes réponses…", "en": "Taking your answers into account…"},
}


def t(key: str, lang: Lang) -> str:
    return _T.get(key, _T["generic"])[lang]


_TOOL_STEP: dict[str, str] = {
    "ask_clarifications": "clarify",
    "plan_campaign": "plan",
    "create_campaign": "launch",
    "find_more_leads": "launch",
    "find_decision_makers": "launch",
    "rerun_campaign": "launch",
    "pause_campaign": "pause",
    "resume_campaign": "resume",
    "cancel_campaign": "cancel",
    "amend_campaign": "amend",
    "get_campaign_status": "status",
    "filter_table": "filter",
    "sort_table": "sort",
    "create_column": "column",
    "enrich_column": "enrich",
    "refresh_column": "enrich",
    "find_emails": "emails",
    "verify_emails": "verify",
    "create_list": "lists",
    "add_to_list": "lists",
    "remove_from_list": "lists",
    "move_between_lists": "lists",
    "rename_list": "lists",
    "archive_list": "lists",
    "duplicate_list": "lists",
    "export_leads": "export",
    "get_lists": "read",
    "get_list": "read",
    "query_leads": "read",
    "get_sources": "read",
    "get_lead_history": "read",
    "explain_score": "read",
}


def tool_step(name: str, args: dict[str, Any] | None, lang: Lang) -> str:
    """A short sentence for a tool call, enriched with its arguments when that helps."""
    key = _TOOL_STEP.get(name, "generic")
    a = args or {}
    if name == "create_column" and a.get("name"):
        return (
            f"Je prépare la colonne « {a['name']} »…"
            if lang == "fr"
            else f"Setting up the “{a['name']}” column…"
        )
    if name in ("add_to_list", "move_between_lists") and (a.get("list_name") or a.get("to_list_name")):
        target = a.get("list_name") or a.get("to_list_name")
        return f"J'ajoute à « {target} »…" if lang == "fr" else f"Adding to “{target}”…"
    return t(key, lang)


def search_sentence(industries: list[str], location: str | None, sources: list[str], lang: Lang) -> str:
    """'Recherche d'agences marketing à Lyon (Google Maps, annuaire)…'"""
    what = industries[0] if industries else ("entreprises" if lang == "fr" else "companies")
    srcs = ", ".join(source_label(s, lang) for s in sources[:3])
    if lang == "fr":
        s = f"Recherche de {what}"
        if location:
            s += f" à {location}"
    else:
        s = f"Searching {what}"
        if location:
            s += f" in {location}"
    return s + (f" ({srcs})…" if srcs else "…")
