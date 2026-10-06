"""Gemini implementation of AIProvider (google-genai SDK). The only module importing the SDK."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from scout.ai.models import ModelRole, estimate_cost, model_for
from scout.ai.provider import (
    AIResult,
    AIUsage,
    ChatEvent,
    ChatTurn,
    GroundedResult,
    GroundingSource,
    GroundingSupport,
    TextDelta,
    ToolCallEvent,
    ToolCallRequest,
    ToolSpec,
    TurnComplete,
)
from scout.config import get_settings
from scout.db.enums import UsageCategory
from scout.errors import AIUnavailable, RetryableError
from scout.services.usage import record_usage
from scout.util.pools import pool

T = TypeVar("T", bound=BaseModel)


def _usage(model: str, resp: Any, grounded: int = 0) -> AIUsage:
    um = getattr(resp, "usage_metadata", None)
    tin = int(getattr(um, "prompt_token_count", 0) or 0) + int(
        getattr(um, "tool_use_prompt_token_count", 0) or 0
    )
    tout = int(getattr(um, "candidates_token_count", 0) or 0) + int(
        getattr(um, "thoughts_token_count", 0) or 0
    )
    cost = estimate_cost(model, tin, tout) + grounded * get_settings().cost_grounded_search_usd
    return AIUsage(model=model, tokens_in=tin, tokens_out=tout, cost_usd=cost, grounded_queries=grounded)


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic JSON schema → Gemini response_json_schema (drop unsupported keys)."""
    drop = {"title", "default", "examples"}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items() if k not in drop}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


class GeminiProvider:
    name = "gemini"

    def __init__(self) -> None:
        key = get_settings().gemini_api_key
        if not key or not key.get_secret_value():
            raise AIUnavailable("GEMINI_API_KEY is not configured")
        self._client = genai.Client(
            api_key=key.get_secret_value(),
            http_options=types.HttpOptions(timeout=int(get_settings().ai_timeout_seconds * 1000)),
        )

    @property
    def available(self) -> bool:
        return True

    async def _record(self, usage: AIUsage, resolver: str) -> None:
        await record_usage(
            UsageCategory.ai_tokens,
            cost_usd=usage.cost_usd - usage.grounded_queries * get_settings().cost_grounded_search_usd,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            model=usage.model,
            resolver=resolver,
        )
        if usage.grounded_queries:
            await record_usage(
                UsageCategory.grounded_search,
                cost_usd=usage.grounded_queries * get_settings().cost_grounded_search_usd,
                quantity=usage.grounded_queries,
                model=usage.model,
                resolver=resolver,
            )

    async def structured(
        self, *, role: ModelRole, system: str, prompt: str, schema: type[T], temperature: float = 0.0
    ) -> AIResult[T]:
        model = model_for(role)
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            response_mime_type="application/json",
            response_json_schema=_strict_schema(schema.model_json_schema()),
        )
        async with pool("gemini"):
            try:
                resp = await self._client.aio.models.generate_content(
                    model=model, contents=prompt, config=config
                )
            except Exception as exc:  # SDK raises APIError subclasses
                raise RetryableError(f"Gemini request failed: {exc}") from exc
        usage = _usage(model, resp)
        await self._record(usage, resolver=f"structured:{schema.__name__}")
        text = resp.text or ""
        try:
            value = schema.model_validate_json(text)
        except ValidationError as exc:
            raise RetryableError(f"Gemini output failed schema validation: {exc.errors()[:2]}") from exc
        return AIResult(value=value, usage=usage, raw_text=text)

    async def chat_stream(
        self, *, role: ModelRole, system: str, turns: list[ChatTurn], tools: list[ToolSpec]
    ) -> AsyncIterator[ChatEvent]:
        model = model_for(role)
        contents: list[types.Content] = []
        for t in turns:
            if t.role == "user":
                contents.append(types.Content(role="user", parts=[types.Part(text=t.text)]))
            elif t.role == "assistant":
                if isinstance(t.provider_state, types.Content):
                    contents.append(t.provider_state)  # preserves thought signatures
                else:
                    parts = [types.Part(text=t.text)] if t.text else []
                    parts += [
                        types.Part(function_call=types.FunctionCall(id=c.id, name=c.name, args=c.args))
                        for c in t.tool_calls
                    ]
                    if parts:
                        contents.append(types.Content(role="model", parts=parts))
            else:
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    id=c.id, name=c.name, response=result
                                )
                            )
                            for c, result in t.tool_results
                        ],
                    )
                )
        decls = [
            types.FunctionDeclaration(
                name=t.name, description=t.description, parameters_json_schema=t.parameters
            )
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
            tools=[types.Tool(function_declarations=decls)] if decls else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        text_parts: list[str] = []
        calls: list[ToolCallRequest] = []
        model_parts: list[types.Part] = []
        last_resp: Any = None
        async with pool("gemini"):
            try:
                stream = await self._client.aio.models.generate_content_stream(
                    model=model, contents=contents, config=config
                )
                async for chunk in stream:
                    last_resp = chunk
                    cand = chunk.candidates[0] if chunk.candidates else None
                    if cand is None or cand.content is None:
                        continue
                    for part in cand.content.parts or []:
                        model_parts.append(part)
                        if part.text and not part.thought:
                            text_parts.append(part.text)
                            yield TextDelta(part.text)
                        if part.function_call:
                            fc = part.function_call
                            call = ToolCallRequest(
                                id=fc.id or f"call_{len(calls)}", name=fc.name or "", args=dict(fc.args or {})
                            )
                            calls.append(call)
                            yield ToolCallEvent(call)
            except Exception as exc:
                raise RetryableError(f"Gemini chat failed: {exc}") from exc
        usage = _usage(model, last_resp)
        await self._record(usage, resolver="chat")
        turn = ChatTurn(
            role="assistant",
            text="".join(text_parts),
            tool_calls=calls,
            provider_state=types.Content(role="model", parts=model_parts),
        )
        yield TurnComplete(turn=turn, usage=usage)

    async def grounded_search(
        self, *, query: str, instructions: str, schema: type[T] | None = None
    ) -> GroundedResult[T]:
        model = model_for(ModelRole.search)
        prompt = f"{instructions}\n\nResearch question: {query}"
        if schema is not None:
            prompt += (
                "\n\nAnswer ONLY with a JSON object matching this JSON schema (no prose, no code fences):\n"
                + json.dumps(_strict_schema(schema.model_json_schema()))
            )
        config = types.GenerateContentConfig(
            temperature=0.0,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        )
        async with pool("search"):
            try:
                resp = await self._client.aio.models.generate_content(
                    model=model, contents=prompt, config=config
                )
            except Exception as exc:
                raise RetryableError(f"Gemini grounded search failed: {exc}") from exc
        cand = resp.candidates[0] if resp.candidates else None
        gm = getattr(cand, "grounding_metadata", None) if cand else None
        sources: list[GroundingSource] = []
        supports: list[GroundingSupport] = []
        queries: list[str] = []
        if gm is not None:
            queries = list(gm.web_search_queries or [])
            for ch in gm.grounding_chunks or []:
                if ch.web and ch.web.uri:
                    sources.append(GroundingSource(uri=ch.web.uri, title=ch.web.title, domain=ch.web.domain))
            for sp in gm.grounding_supports or []:
                if sp.segment and sp.segment.text:
                    supports.append(
                        GroundingSupport(
                            text=sp.segment.text, source_indices=list(sp.grounding_chunk_indices or [])
                        )
                    )
        usage = _usage(model, resp, grounded=max(1, len(queries)) if gm is not None else 0)
        await self._record(usage, resolver="grounded_search")
        text = resp.text or ""
        value: T | None = None
        if schema is not None:
            raw = text.strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                raw = raw[raw.find("{") :]
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                try:
                    value = schema.model_validate_json(raw[start : end + 1])
                except ValidationError:
                    value = None
        return GroundedResult(
            text=text, value=value, sources=sources, search_queries=queries, supports=supports, usage=usage
        )
