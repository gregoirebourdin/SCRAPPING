"""Social profile URL normalization and extraction from cached pages (deterministic)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from scout.crawl.types import PageLike
from scout.db.enums import PageType

SOCIAL_NETWORKS = ("linkedin", "instagram", "facebook", "x", "tiktok", "youtube", "pinterest", "threads")
# Personal LinkedIn profiles (/in/…) are reported under this pseudo-network: they belong to
# people, not to the company.
LINKEDIN_PERSON = "linkedin_profile"

_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")

_FACEBOOK_RESERVED = {
    "sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "login", "login.php", "home.php",
    "watch", "photo", "photo.php", "photos", "story.php", "permalink.php", "hashtag", "events", "groups",
    "gaming", "marketplace", "help", "privacy", "policies", "legal", "settings", "notes", "media", "l.php",
    "business", "ads", "pg", "public", "search", "people", "messages", "video.php", "reel", "reels", "stories",
}
_INSTAGRAM_RESERVED = {
    "p", "reel", "reels", "tv", "explore", "stories", "accounts", "direct", "about", "legal", "developer",
    "web", "emails", "share", "s",
}
_X_RESERVED = {
    "intent", "share", "home", "search", "hashtag", "i", "explore", "settings", "login", "signup",
    "messages", "notifications", "privacy", "tos", "about", "compose", "widgets.js", "status",
}
_PINTEREST_RESERVED = {"pin", "search", "ideas", "today", "categories", "explore", "business", "_", "login", "settings"}
_YOUTUBE_RESERVED = {
    "watch", "embed", "playlist", "shorts", "results", "feed", "live", "redirect", "about", "t", "gaming",
    "premium", "account", "subscription_center", "attribution_link", "share",
}


def _first_segments(path: str, n: int = 3) -> list[str]:
    return [unquote(s) for s in path.split("/") if s][:n]


def _host(url: str) -> tuple[str, str, dict[str, list[str]]] | None:
    raw = (url or "").strip()
    if not raw:
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    elif "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().strip(".")
    for prefix in ("www.", "m.", "mobile.", "web.", "business.", "l.", "lm.", "touch."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    # Localized subdomains: fr-fr.facebook.com, fr.linkedin.com, fr.pinterest.com …
    host = re.sub(r"^[a-z]{2}(?:-[a-z]{2})?\.(facebook|linkedin|pinterest)\.", r"\1.", host)
    return host, parts.path or "/", parse_qs(parts.query)


def normalize_social_url(url: str) -> tuple[str, str] | None:
    """Return ``(network, canonical_profile_url)`` or None for share/intent/post/status links.

    Personal LinkedIn profiles return ``("linkedin_profile", url)``.
    """
    parsed = _host(url)
    if parsed is None:
        return None
    host, path, query = parsed
    segs = _first_segments(path)
    low = [s.lower() for s in segs]

    if host in ("linkedin.com",):
        if len(low) >= 2 and low[0] in ("company", "school", "showcase") and _HANDLE_RE.match(segs[1]):
            return "linkedin", f"https://www.linkedin.com/{low[0]}/{segs[1].lower()}/"
        if len(low) >= 2 and low[0] in ("in", "pub") and _HANDLE_RE.match(segs[1]):
            return LINKEDIN_PERSON, f"https://www.linkedin.com/in/{segs[1].lower()}/"
        return None

    if host in ("instagram.com", "instagr.am"):
        if low and low[0] not in _INSTAGRAM_RESERVED and _HANDLE_RE.match(segs[0]) and len(low) == 1:
            return "instagram", f"https://www.instagram.com/{segs[0].lower()}/"
        return None

    if host in ("facebook.com", "fb.com", "fb.me"):
        if not low:
            return None
        if low[0] == "profile.php":
            ids = query.get("id")
            if ids and ids[0].isdigit():
                return "facebook", f"https://www.facebook.com/profile.php?id={ids[0]}"
            return None
        if low[0] == "pages" and len(segs) >= 2:
            return "facebook", "https://www.facebook.com/pages/" + "/".join(segs[1:3])
        if low[0] in _FACEBOOK_RESERVED or low[0].endswith(".php"):
            return None
        if len(low) >= 2 and low[1] in ("posts", "videos", "photos", "events", "reviews") and len(low) > 2:
            return None
        if _HANDLE_RE.match(segs[0]):
            return "facebook", f"https://www.facebook.com/{segs[0]}"
        return None

    if host in ("twitter.com", "x.com"):
        if not low or low[0] in _X_RESERVED or not _HANDLE_RE.match(segs[0]):
            return None
        if len(low) >= 2 and low[1] in ("status", "statuses"):
            return None
        return "x", f"https://x.com/{segs[0]}"

    if host == "tiktok.com":
        if low and low[0].startswith("@") and len(low) == 1 and _HANDLE_RE.match(segs[0][1:] or "-"):
            return "tiktok", f"https://www.tiktok.com/{segs[0].lower()}"
        return None

    if host in ("youtube.com", "youtube-nocookie.com"):
        if not low or low[0] in _YOUTUBE_RESERVED:
            return None
        if low[0] in ("channel", "c", "user") and len(segs) >= 2 and _HANDLE_RE.match(segs[1]):
            return "youtube", f"https://www.youtube.com/{low[0]}/{segs[1]}"
        if low[0].startswith("@") and _HANDLE_RE.match(segs[0][1:] or "-"):
            return "youtube", f"https://www.youtube.com/{segs[0]}"
        return None

    if re.match(r"^pinterest\.[a-z.]{2,6}$", host) or host == "pin.it":
        if host == "pin.it" or not low or low[0] in _PINTEREST_RESERVED or not _HANDLE_RE.match(segs[0]):
            return None
        if len(low) >= 2 and low[1] in ("pin",):
            return None
        return "pinterest", f"https://www.pinterest.com/{segs[0].lower()}/"

    if host in ("threads.net", "threads.com"):
        if low and low[0].startswith("@") and _HANDLE_RE.match(segs[0][1:] or "-") and len(low) == 1:
            return "threads", f"https://www.threads.net/{segs[0].lower()}"
        return None
    return None


def _same_as_urls(structured: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for obj in structured or []:
        if not isinstance(obj, dict):
            continue
        same = obj.get("sameAs")
        if isinstance(same, str):
            out.append(same)
        elif isinstance(same, list):
            out.extend(s for s in same if isinstance(s, str))
    return out


def _page_rank(page: PageLike) -> int:
    pt = str(page.page_type)
    order = {PageType.home.value: 0, PageType.contact.value: 1, PageType.about.value: 2, PageType.team.value: 3}
    return order.get(pt, 9)


def extract_social_profiles(pages: list[PageLike]) -> dict[str, tuple[str, str]]:
    """Company social profiles: ``{network: (profile_url, source_page_url)}``; home page links win."""
    found: dict[str, tuple[str, str]] = {}
    for page in sorted(pages, key=_page_rank):
        links = page.links or {}
        candidates: list[str] = []
        social = links.get("social") if isinstance(links, dict) else None
        if isinstance(social, dict):
            candidates.extend(v for v in social.values() if isinstance(v, str))
        candidates.extend(_same_as_urls(page.structured_data))
        for raw in candidates:
            norm = normalize_social_url(raw)
            if norm is None:
                continue
            network, canonical = norm
            if network == LINKEDIN_PERSON or network in found:
                continue
            found[network] = (canonical, page.url)
    return found
