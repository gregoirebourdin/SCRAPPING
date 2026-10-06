"""Technology detection contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DetectedTech:
    name: str  # canonical name (see scout.tech.builtin.KNOWN_TECHNOLOGIES)
    category: str
    version: str | None
    confidence: float  # 0–1
    evidence: str  # verbatim matched fragment (header, tag, URL)
