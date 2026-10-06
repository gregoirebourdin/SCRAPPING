"""Demo suite (tests and local development only — not loaded in production).

Ten labelled name pairs, two matching strategies: ``exact`` (raw string equality) and ``normalized``
(the harness's own person-name matcher). Deterministic, no I/O, no cost. It exercises the suite plumbing
(strategies side by side, per-item diffs, Wilson intervals); it says nothing about lead quality.
"""

from __future__ import annotations

import time
from typing import Any

from scout.benchmark.metrics import name_tokens, names_match, percentile, rate
from scout.benchmark.registry import SuiteResult, register_suite

# (expected name, observed name, same person?)
PAIRS: list[tuple[str, str, bool]] = [
    ("Marie Dupont", "Marie Dupont", True),
    ("Élodie Lefèbvre", "Elodie Lefebvre", True),
    ("Jean-Pierre Martin", "Jean Pierre Martin", True),
    ("DUPONT Marie", "Marie Dupont", True),
    ("Anne Marie Roux", "Anne Roux", True),
    ("Marie Dupont", "Marine Dupont", False),
    ("Paul Durand", "Paul Durant", False),
    ("Léa Simon", "Lea Simon", True),
    ("Hugo Moreau", "Hugo Morel", False),
    ("Chloé N'Diaye", "Chloe N Diaye", True),
]
STRATEGIES = ("exact", "normalized")


def _match(strategy: str, a: str, b: str) -> bool:
    if strategy == "exact":
        return a == b
    return names_match(name_tokens(full=a), name_tokens(full=b))


async def run(config: dict[str, Any]) -> SuiteResult:
    wanted = [s for s in (config.get("strategies") or STRATEGIES) if s in STRATEGIES]
    strategies: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    for strategy in wanted:
        tp = fp = fn = 0
        latencies: list[float] = []
        for i, (a, b, same) in enumerate(PAIRS):
            t0 = time.perf_counter()
            got = _match(strategy, a, b)
            latencies.append((time.perf_counter() - t0) * 1000)
            tp += got and same
            fp += got and not same
            fn += same and not got
            items.append(
                {
                    "label": f"#{i + 1} {a} ↔ {b}",
                    "strategy": strategy,
                    "expected": {"same_person": same},
                    "actual": {"same_person": got},
                    "verdict": "correct" if got == same else ("false_positive" if got else "false_negative"),
                    "fp": int(got and not same),
                    "fn": int(same and not got),
                    "latency_ms": 0,
                    "cost_usd": 0.0,
                }
            )
        strategies[strategy] = {
            "person_precision": rate(tp, tp + fp),
            "person_recall": rate(tp, tp + fn),
            "false_positives": fp,
            "false_negatives": fn,
            "p95_processing_ms": percentile(latencies, 95),
        }
    best = strategies.get("normalized") or next(iter(strategies.values()), {})
    return SuiteResult(
        metrics={**best, "items": len(PAIRS)},
        strategies=strategies,
        items=items,
        notes=["Demo suite: labelled name pairs only — no statement about lead quality."],
        labels={"person_precision": "Name-match precision", "person_recall": "Name-match recall"},
        definitions={
            "person_precision": "Pairs judged the same person that are labelled the same person / pairs judged the same.",
            "person_recall": "Pairs labelled the same person that were judged the same / pairs labelled the same.",
            "false_positives": "Pairs wrongly judged the same person.",
            "false_negatives": "Pairs of the same person that were not matched.",
            "p95_processing_ms": "95th percentile matching time per pair.",
            "items": "Labelled pairs.",
        },
    )


register_suite(
    "demo",
    "Demo · name matching",
    "Ten labelled name pairs, exact vs normalized matching. Plumbing check only (no I/O, no cost).",
    run,
    strategies=STRATEGIES,
    demo=True,
)
