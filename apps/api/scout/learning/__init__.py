"""Empirical Source Scoring (docs/ARCHITECTURE.md §Empirical Source Scoring).

Learns, per resolver / source, how often it is tried (attempts), produces a usable result (coverage), turns
out right or wrong (precision), how long it takes and what it costs — and feeds that back into source routing
(``scout.discovery.router``), confidence scoring (people, enrichment cells, email resolvers), enrichment
planning and fallback order (``scout.enrich.planner``).

* ``stats``    — counters (``resolver_stats``), ``record`` / ``record_outcome`` / ``snapshot`` / ``precision`` /
  ``coverage`` (never raise);
* ``priors``   — default priors per (dimension, key): the only place starting values live;
* ``routing``  — min-evidence gated estimates, ranking with exploration, confidence blending;
* ``people`` / ``feedback`` / ``report`` — pipeline helpers, user feedback → outcomes, admin report.
"""

from __future__ import annotations

from scout.learning.stats import (
    PRIOR_STRENGTH,
    StatEvent,
    StatRow,
    coverage,
    describe,
    precision,
    record,
    record_outcome,
    snapshot,
)

__all__ = [
    "PRIOR_STRENGTH",
    "StatEvent",
    "StatRow",
    "coverage",
    "describe",
    "precision",
    "record",
    "record_outcome",
    "snapshot",
]
