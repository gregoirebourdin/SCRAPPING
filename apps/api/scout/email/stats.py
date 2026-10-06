"""Empirical resolver statistics for the email engine (docs/EMAIL_ENGINE.md §Empirical scoring).

Compatibility module: the implementation moved to :mod:`scout.learning.stats` (generic Empirical Source
Scoring over ``resolver_stats``). Every name the email engine used is re-exported unchanged.

Every resolution records an attempt per dimension (resolver, source, pattern, technique, provider);
outcomes are recorded when ground truth appears:

* confirmed correct — SMTP accepted the chosen address on a domain proven not catch-all (healthy
  infrastructure), the user confirmed it, or it was later observed published for that person;
* confirmed wrong — SMTP rejected it as an unknown user (healthy infrastructure, not catch-all) or the
  user replaced it with a different address.

``precision()`` turns the counts into a Beta-smoothed precision around the code's default prior, so
the defaults are only a starting point and the engine learns which resolvers actually work.
"""

from __future__ import annotations

from scout.db.models import EmailResolverStat, ResolverStat
from scout.learning.stats import (
    PRIOR_STRENGTH,
    SNAPSHOT_TTL_S,
    StatEvent,
    StatRow,
    cached_snapshot,
    coverage,
    describe,
    precision,
    record,
    record_outcome,
    reset_cache,
    snapshot,
)

__all__ = [
    "PRIOR_STRENGTH",
    "SNAPSHOT_TTL_S",
    "EmailResolverStat",
    "ResolverStat",
    "StatEvent",
    "StatRow",
    "cached_snapshot",
    "coverage",
    "describe",
    "precision",
    "record",
    "record_outcome",
    "reset_cache",
    "snapshot",
]
