"""Provider-neutral AI interface. Business logic depends on this module only (spec §135)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel

from scout.ai.models import ModelRole

T = TypeVar("T", bound=BaseModel)


@dataclass
class AIUsage:
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    grounded_queries: int = 0


@dataclass
class AIResult(Generic[T]):
    value: T
    usage: AIUsage
    raw_text: str | None = None


@dataclass
class GroundingSource:
    uri: str
    title: str | None = None
    domain: str | None = None


@dataclass
class GroundingSupport:
    text: str
    source_indices: list[int]


@dataclass
class GroundedResult(Generic[T]):
    text: str
    value: T | None
    sources: list[GroundingSource]
    search_queries: list[str]
    supports: list[GroundingSupport]
    usage: AIUsage


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema (object)


@dataclass
class ToolCallRequest:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ChatTurn:
    """Provider-neutral conversation turn.

    role=user|assistant: text content. role=tool: results for the previous assistant tool calls.
    `provider_state` lets a provider round-trip opaque data (e.g. Gemini thought signatures)
    within a single operator loop; it is never persisted.
    """

    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_results: list[tuple[ToolCallRequest, dict[str, Any]]] = field(default_factory=list)
    provider_state: Any = None


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallEvent:
    call: ToolCallRequest


@dataclass
class TurnComplete:
    turn: ChatTurn  # assistant turn as produced (text + tool calls + provider state)
    usage: AIUsage


ChatEvent = TextDelta | ToolCallEvent | TurnComplete


class AIProvider(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    async def structured(
        self,
        *,
        role: ModelRole,
        system: str,
        prompt: str,
        schema: type[T],
        temperature: float = 0.0,
    ) -> AIResult[T]: ...

    def chat_stream(
        self,
        *,
        role: ModelRole,
        system: str,
        turns: list[ChatTurn],
        tools: list[ToolSpec],
    ) -> AsyncIterator[ChatEvent]: ...

    async def grounded_search(
        self,
        *,
        query: str,
        instructions: str,
        schema: type[T] | None = None,
    ) -> GroundedResult[T]: ...
