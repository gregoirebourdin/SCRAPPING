"""Provider selection. `get_ai()` returns Gemini when configured, otherwise the deterministic local provider."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from scout.ai.models import ModelRole
from scout.ai.provider import (
    AIProvider,
    AIResult,
    AIUsage,
    ChatEvent,
    ChatTurn,
    GroundedResult,
    ToolSpec,
)
from scout.config import get_settings
from scout.errors import AIUnavailable

T = TypeVar("T", bound=BaseModel)

# The local chat router is registered by scout.chat.local_router to avoid an import cycle.
LocalChatHandler = Callable[[str, list[ChatTurn], list[ToolSpec]], AsyncIterator[ChatEvent]]
_local_chat_handler: LocalChatHandler | None = None


def register_local_chat_handler(handler: LocalChatHandler) -> None:
    global _local_chat_handler
    _local_chat_handler = handler


class LocalProvider:
    """Zero-cost deterministic fallback (no API key). Callers use deterministic code paths when
    `available` is False; chat is served by the rule-based local command router."""

    name = "local"

    @property
    def available(self) -> bool:
        return False

    async def structured(self, *, role: ModelRole, system: str, prompt: str, schema: type[T], temperature: float = 0.0) -> AIResult[T]:
        raise AIUnavailable("AI provider not configured (set GEMINI_API_KEY)")

    async def chat_stream(
        self, *, role: ModelRole, system: str, turns: list[ChatTurn], tools: list[ToolSpec]
    ) -> AsyncIterator[ChatEvent]:
        if _local_chat_handler is None:
            raise AIUnavailable("local chat router not registered")
        async for ev in _local_chat_handler(system, turns, tools):
            yield ev

    async def grounded_search(self, *, query: str, instructions: str, schema: type[T] | None = None) -> GroundedResult[T]:
        raise AIUnavailable("Grounded search requires GEMINI_API_KEY")


class FakeProvider:
    """Test double: scripted structured responses by schema name; chat delegates to the local router."""

    name = "fake"

    def __init__(self) -> None:
        self.structured_handlers: dict[str, Callable[[str], Any]] = {}
        self.grounded_handler: Callable[[str, type[Any] | None], GroundedResult[Any]] | None = None
        self.calls: list[tuple[str, str]] = []

    @property
    def available(self) -> bool:
        return True

    def on(self, schema_name: str, handler: Callable[[str], Any]) -> None:
        self.structured_handlers[schema_name] = handler

    async def structured(self, *, role: ModelRole, system: str, prompt: str, schema: type[T], temperature: float = 0.0) -> AIResult[T]:
        self.calls.append((schema.__name__, prompt))
        handler = self.structured_handlers.get(schema.__name__)
        if handler is None:
            raise AIUnavailable(f"FakeProvider has no handler for {schema.__name__}")
        value = handler(prompt)
        if not isinstance(value, schema):
            value = schema.model_validate(value)
        return AIResult(value=value, usage=AIUsage(model="fake", tokens_in=len(prompt) // 4, tokens_out=50))

    async def chat_stream(
        self, *, role: ModelRole, system: str, turns: list[ChatTurn], tools: list[ToolSpec]
    ) -> AsyncIterator[ChatEvent]:
        if _local_chat_handler is None:
            raise AIUnavailable("local chat router not registered")
        async for ev in _local_chat_handler(system, turns, tools):
            yield ev

    async def grounded_search(self, *, query: str, instructions: str, schema: type[T] | None = None) -> GroundedResult[T]:
        if self.grounded_handler is None:
            raise AIUnavailable("FakeProvider has no grounded handler")
        return self.grounded_handler(query, schema)


_provider: AIProvider | None = None


def get_ai() -> AIProvider:
    global _provider
    if _provider is None:
        kind = get_settings().resolved_ai_provider
        if kind == "gemini":
            from scout.ai.gemini import GeminiProvider

            _provider = GeminiProvider()
        elif kind == "fake":
            _provider = FakeProvider()
        else:
            _provider = LocalProvider()
    return _provider


def set_ai(provider: AIProvider | None) -> None:
    """Tests / dependency injection."""
    global _provider
    _provider = provider
