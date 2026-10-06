"""Enrichment resolvers (``enrich.resolver``, keyed by plan strategy): attempts and learned cell confidence.

Used by ``scout.enrich.engine._Batch``: one attempt per computed cell (restored / skipped cells are not
attempts), produced = status success, latency of the resolver call, ``CellResult.cost_usd``. When
``empirical_routing_enabled``, a successful factual cell's confidence is shifted by the strategy's learned
precision (``routing.blend_confidence``). Never raises.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from scout.learning import routing
from scout.learning import stats as S

log = structlog.get_logger("learning.enrich")

DIMENSION = "enrich.resolver"
NO_CONFIDENCE_BLEND = frozenset({"generated_text"})  # generated copy is not evidence


class EnrichLearning:
    def __init__(self, snap: S.Snapshot | None = None, *, use: bool = True) -> None:
        self.snap = snap or {}
        self.use = use
        self.events: list[S.StatEvent] = []

    @classmethod
    async def start(cls) -> EnrichLearning:
        use = routing.enabled()
        return cls(await S.snapshot() if use else {}, use=use)

    def observe(self, strategy: str, result: Any, started: float) -> None:
        """Record the attempt and blend the cell confidence in place."""
        try:
            status = str(getattr(result.status, "value", result.status))
            self.events.append(
                S.StatEvent(
                    DIMENSION,
                    strategy,
                    produced=status == "success",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    cost_usd=float(result.cost_usd or 0.0),
                )
            )
            if (
                self.use
                and self.snap
                and status == "success"
                and strategy not in NO_CONFIDENCE_BLEND
                and isinstance(result.confidence, int | float)
            ):
                result.confidence = routing.learned_confidence(
                    DIMENSION, strategy, float(result.confidence), self.snap
                )
        except Exception as exc:  # pragma: no cover - defensive
            log.info("learning.enrich_observe_failed", error=str(exc))

    async def flush(self) -> None:
        events, self.events = self.events, []
        await S.record(events)
