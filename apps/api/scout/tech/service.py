"""Client for the Go fingerprint service (wappalyzergo, ``POST /v1/tech``).

The service runs on the private network (Railway service B) and is configured by the operator,
so it is reached with a plain client (not the SSRF-guarded crawler client). Any failure returns
``None`` so callers fall back to the built-in detector.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from scout.config import get_settings
from scout.tech.builtin import CATEGORIES, canonical_tech_name
from scout.tech.types import DetectedTech

log = structlog.get_logger(__name__)

SERVICE_TIMEOUT_S = 10.0
SERVICE_CONFIDENCE = 0.9
MAX_HTML_BYTES = 200_000


def service_url() -> str | None:
    s = get_settings()
    base = s.tech_service_url or s.verifier_service_url
    return base.rstrip("/") if base else None


def _parse(payload: Any) -> list[DetectedTech] | None:
    items = payload.get("technologies") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return None
    out: list[DetectedTech] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            continue
        raw_name = item["name"].strip()
        name = canonical_tech_name(raw_name) or raw_name
        if name in seen:
            continue
        seen.add(name)
        cats = item.get("categories") or []
        category = (
            cats[0]
            if isinstance(cats, list) and cats and isinstance(cats[0], str)
            else CATEGORIES.get(name, "Other")
        )
        version = (
            item.get("version") if isinstance(item.get("version"), str) and item.get("version") else None
        )
        out.append(
            DetectedTech(
                name=name,
                category=category,
                version=version,
                confidence=SERVICE_CONFIDENCE,
                evidence=f"wappalyzergo fingerprint ({raw_name})",
            )
        )
    return out


async def detect_via_service(
    url: str, headers: dict[str, Any] | None, html: str | None
) -> list[DetectedTech] | None:
    """Technologies from the Go service, or None when it is not configured / fails."""
    base = service_url()
    if not base:
        return None
    s = get_settings()
    req_headers = {"Content-Type": "application/json"}
    if s.verifier_service_token is not None and s.verifier_service_token.get_secret_value():
        req_headers["Authorization"] = f"Bearer {s.verifier_service_token.get_secret_value()}"
    body = {
        "url": url,
        "headers": {str(k): str(v) for k, v in (headers or {}).items()},
        "html": (html or "")[:MAX_HTML_BYTES],
    }
    try:
        async with httpx.AsyncClient(timeout=SERVICE_TIMEOUT_S, trust_env=False) as client:
            resp = await client.post(f"{base}/v1/tech", json=body, headers=req_headers)
        if resp.status_code != 200:
            log.info("tech_service_error", status=resp.status_code)
            return None
        return _parse(resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        log.info("tech_service_unavailable", error=str(exc)[:200])
        return None
