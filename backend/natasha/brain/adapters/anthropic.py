"""Anthropic (Claude) adapter - the Messages API."""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Iterable

import httpx

from .base import ChatMessage, ChatResponse, ModelDescriptor, ProviderAdapter, StreamChunk, ToolCall

KNOWN = {
    "claude-3-5-sonnet": {"context": 200_000, "quality": 0.92, "vision": True},
    "claude-3-5-haiku": {"context": 200_000, "quality": 0.75, "vision": True},
    "claude-sonnet-4": {"context": 200_000, "quality": 0.93, "vision": True},
    "claude-opus-4": {"context": 200_000, "quality": 0.95, "vision": True},
}


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"
    supports_vision = True
    supports_tools = True
    supports_embeddings = False
    default_model = "claude-3-5-sonnet-latest"
    models = ["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.default_model = str(getattr(self.settings, "default_model", "") or self.default_model)
        self._version = "2023-06-01"

    def _headers(self, *, include_auth: bool = True) -> dict[str, str]:
        headers = super()._headers(include_auth=include_auth)
        headers["anthropic-version"] = self._version
        if include_auth and self.credential_provider is not None and self.credential_ref:
            try:
                secret = self.credential_provider(self.credential_ref, purpose="provider:anthropic")
                token = secret.get("x-api-key") or secret.get("Authorization", "").replace("Bearer ", "")
                if token:
                    headers["x-api-key"] = token
                    headers.pop("Authorization", None)
            except Exception:
                pass
        return headers

    def _split(self, messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
        system = "\n\n".join(message.content for message in messages if message.role == "system")
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                converted.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": message.tool_call_id, "content": message.content}]})
                continue
            if message.tool_calls:
                blocks = [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in message.tool_calls]
                if message.content:
                    blocks.insert(0, {"type": "text", "text": message.content})
                converted.append({"role": "assistant", "content": blocks})
                continue
            if message.images:
                blocks: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
                for image in message.images:
                    if image.startswith("data:"):
                        header, _, payload = image.partition(",")
                        media_type = header.split(";")[0].replace("data:", "") or "image/png"
                        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": payload}})
                converted.append({"role": message.role, "content": blocks})
            else:
                converted.append({"role": message.role, "content": message.content})
        return system, converted

    async def list_models(self) -> list[ModelDescriptor]:
        async with httpx.AsyncClient(timeout=min(self.timeout, 30.0)) as client:
            response = await client.get(f"{self.base_url.rstrip('/')}/v1/models", headers=self._headers())
            if response.status_code >= 400:
                return [ModelDescriptor(id=model, provider=self.name, context_window=200_000, supports_vision=True,
                                        quality=_quality(model)) for model in self.models]
            data = response.json().get("data", [])
        return [
            ModelDescriptor(id=item.get("id", ""), provider=self.name, context_window=item.get("context_window", 200_000),
                            supports_vision=True, supports_tools=True, quality=_quality(item.get("id", "")))
            for item in data if item.get("id")
        ] or [ModelDescriptor(id=model, provider=self.name, context_window=200_000) for model in self.models]

    async def chat(self, messages: list[ChatMessage], *, model: str = "", temperature: float = 0.7,
                   max_tokens: int | None = None, tools: list[dict[str, Any]] | None = None, **params: Any) -> ChatResponse:
        system, converted = self._split(messages)
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": converted,
            "max_tokens": max_tokens or 4096,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {"name": tool["function"]["name"], "description": tool["function"].get("description", ""),
                 "input_schema": tool["function"].get("parameters", {})}
                for tool in tools if "function" in tool
            ]
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url.rstrip('/')}/v1/messages", headers=self._headers(), json=payload)
            response.raise_for_status()
            data = response.json()
        latency = (time.perf_counter() - started) * 1000
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(id=block.get("id", ""), name=block.get("name", ""),
                                           arguments=block.get("input", {}) or {}))
        usage = data.get("usage", {}) or {}
        return ChatResponse(
            content="".join(text_parts), model=data.get("model", model or self.default_model), provider=self.name,
            finish_reason=data.get("stop_reason", "stop"), tool_calls=tool_calls,
            input_tokens=int(usage.get("input_tokens", 0) or 0), output_tokens=int(usage.get("output_tokens", 0) or 0),
            latency_ms=latency, raw=data,
        )

    async def stream_chat(self, messages: list[ChatMessage], *, model: str = "", temperature: float = 0.7,
                          max_tokens: int | None = None, **params: Any) -> AsyncIterator[StreamChunk]:
        system, converted = self._split(messages)
        payload = {"model": model or self.default_model, "messages": converted, "max_tokens": max_tokens or 4096,
                   "temperature": temperature, "stream": True}
        if system:
            payload["system"] = system
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", f"{self.base_url.rstrip('/')}/v1/messages", headers=self._headers(), json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("type")
                    if kind == "content_block_delta":
                        yield StreamChunk(delta=event.get("delta", {}).get("text", ""), model=model or self.default_model, provider=self.name)
                    elif kind == "message_stop":
                        yield StreamChunk(done=True, model=model or self.default_model, provider=self.name, finish_reason="stop")
                        return
                    elif kind == "message_delta":
                        usage = event.get("usage", {}) or {}
                        if usage:
                            yield StreamChunk(done=True, model=model or self.default_model, provider=self.name,
                                              output_tokens=int(usage.get("output_tokens", 0) or 0))

    async def embed(self, texts: Iterable[str], *, model: str = "") -> list[list[float]]:
        raise NotImplementedError("Anthropic does not offer an embeddings endpoint; configure another provider")


def _quality(model_id: str) -> float:
    lowered = model_id.lower()
    for known, info in KNOWN.items():
        if known in lowered:
            return float(info["quality"])
    if "opus" in lowered:
        return 0.95
    if "sonnet" in lowered:
        return 0.9
    if "haiku" in lowered:
        return 0.75
    return 0.7
