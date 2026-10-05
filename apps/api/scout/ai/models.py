"""Model configuration layer: roles → model ids (env-configurable) and price table for cost estimates.

Never reference a model id elsewhere in the codebase; ask for a role instead.
"""

from __future__ import annotations

from enum import StrEnum

from scout.config import get_settings


class ModelRole(StrEnum):
    fast = "fast"
    reasoning = "reasoning"
    search = "search"
    extractor = "extractor"


def model_for(role: ModelRole) -> str:
    s = get_settings()
    return {
        ModelRole.fast: s.ai_model_fast,
        ModelRole.reasoning: s.ai_model_reasoning,
        ModelRole.search: s.ai_model_search,
        ModelRole.extractor: s.ai_model_extractor,
    }[role]


# USD per 1M tokens (input, output) — verified on ai.google.dev/gemini-api/docs/pricing (2026-10-05).
PRICES_PER_M: dict[str, tuple[float, float]] = {
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}
DEFAULT_PRICE = (0.75, 3.75)


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = PRICES_PER_M.get(model, DEFAULT_PRICE)
    return (tokens_in * pin + tokens_out * pout) / 1_000_000
