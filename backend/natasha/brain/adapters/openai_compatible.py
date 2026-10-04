"""OpenAI-compatible adapter.

One adapter covers the large family of servers that speak the OpenAI wire format: OpenAI itself,
Groq, Cerebras, Mistral, xAI, OpenRouter, NVIDIA, Hugging Face inference, vLLM, LM Studio,
llama.cpp's server, and any self-hosted OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Iterable

import httpx

from .base import ChatMessage, ChatResponse, ModelDescriptor, ProviderAdapter, StreamChunk, ToolCall

OPENAI_COMPATIBLE_PROVIDERS = {
    "openai", "groq", "cerebras", "mistral", "xai", "openrouter", "nvidia", "huggingface",
    "vllm", "lmstudio", "llamacpp", "openai_compatible", "custom",
}

# Known context windows / capabilities for common models; unknown models fall back to defaults.
KNOWN_MODELS: dict[str, dict[str, Any]] = {
    "gpt-4o": {"context": 128_000, "vision": True, "tools": True, "quality": 0.9},
    "gpt-4o-mini": {"context": 128_000, "vision": True, "tools": True, "quality": 0.75},
    "gpt-4.1": {"context": 1_000_000, "vision": True, "tools": True, "quality": 0.92},
    "o3": {"context": 200_000, "vision": True, "tools": True, "quality": 0.95},
    "llama-3.3-70b-versatile": {"context": 128_000, "tools": True, "quality": 0.8},
    "llama-3.1-8b-instant": {"context": 128_000, "tools": True, "quality": 0.6},
    "mistral-large-latest": {"context": 128_000, "tools": True, "quality": 0.82},
    "mistral-small-latest": {"context": 128_000, "tools": True, "quality": 0.7},
    "grok-2-latest": {"context": 131_072, "vision": True, "tools": True, "quality": 0.85},
}


class OpenAICompatibleAdapter(ProviderAdapter):
    """Adapter for any ``/v1/chat/completions`` server."""

    def __init__(self, name: str = "openai", *args: Any, label: str = "", **kwargs: Any) -> None:
        self.name = name
        self.label = label or name
        super().__init__(*args, **kwargs)
        self.local = bool(getattr(self.settings, "local", False))
        self.privacy_tier = str(getattr(self.settings, "privacy_tier", "local" if self.local else "cloud"))
        self.supports_embeddings = True
        self.default_model = str(getattr(self.settings, "default_model", "") or "")

    # -- helpers --------------------------------------------------------------- #
    def _url(self, path: str) -> str:
        base = (self.base_url or "").rstrip("/")
        if not base:
            raise ValueError(f"provider {self.name!r} has no base_url configured")
        if base.endswith("/v1") and path.startswith("/v1"):
            return base + path[3:]
        return base + path

    def _model(self, model: str) -> str:
        return model or self.default_model or (self.configured_models[0] if self.configured_models else "")

    @staticmethod
    def _content_parts(message: ChatMessage) -> Any:
        if not message.images:
            return message.content
        parts: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
        for image in message.images:
            parts.append({"type": "image_url", "image_url": {"url": image}})
        return parts

    def _payload(self, messages: list[ChatMessage], *, model: str, temperature: float,
                 max_tokens: int | None, tools: list[dict[str, Any]] | None, stream: bool, **params: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model(model),
            "messages": [
                {"role": message.role, "content": self._content_parts(message)}
                | ({"tool_call_id": message.tool_call_id} if message.tool_call_id else {})
                | ({"name": message.name} if message.name and message.role == "tool" else {})
                | ({"tool_calls": [call.to_dict() for call in message.tool_calls]} if message.tool_calls else {})
                for message in messages
            ],
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
        payload.update({k: v for k, v in params.items() if v is not None})
        return payload

    # -- api ------------------------------------------------------------------- #
    async def list_models(self) -> list[ModelDescriptor]:
        async with httpx.AsyncClient(timeout=min(self.timeout, 30.0)) as client:
            response = await client.get(self._url("/v1/models"), headers=self._headers())
            response.raise_for_status()
            data = response.json().get("data", [])
        descriptors: list[ModelDescriptor] = []
        for item in data:
            model_id = item.get("id", "")
            if not model_id:
                continue
            known = _known_for(model_id)
            descriptors.append(
                ModelDescriptor(
                    id=model_id, provider=self.name, context_window=known.get("context", 8192),
                    supports_vision=known.get("vision", self.supports_vision),
                    supports_tools=known.get("tools", self.supports_tools),
                    supports_embeddings="embed" in model_id.lower(),
                    local=self.local, quality=known.get("quality", 0.5),
                )
            )
        if not descriptors:
            descriptors = [ModelDescriptor(id=model, provider=self.name, local=self.local) for model in self.configured_models]
        return descriptors

    async def chat(self, messages: list[ChatMessage], *, model: str = "", temperature: float = 0.7,
                   max_tokens: int | None = None, tools: list[dict[str, Any]] | None = None, **params: Any) -> ChatResponse:
        payload = self._payload(messages, model=model, temperature=temperature, max_tokens=max_tokens,
                                tools=tools, stream=False, **params)
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self._url("/v1/chat/completions"), headers=self._headers(), json=payload)
            response.raise_for_status()
            data = response.json()
        latency = (time.perf_counter() - started) * 1000
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {}) or {}
        usage = data.get("usage", {}) or {}
        return ChatResponse(
            content=message.get("content") or "",
            model=data.get("model", self._model(model)),
            provider=self.name,
            finish_reason=choice.get("finish_reason", "stop"),
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            latency_ms=latency,
            raw=data,
        )

    async def stream_chat(self, messages: list[ChatMessage], *, model: str = "", temperature: float = 0.7,
                          max_tokens: int | None = None, **params: Any) -> AsyncIterator[StreamChunk]:
        payload = self._payload(messages, model=model, temperature=temperature, max_tokens=max_tokens,
                                tools=None, stream=True, **params)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", self._url("/v1/chat/completions"), headers=self._headers(), json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        yield StreamChunk(done=True, model=self._model(model), provider=self.name, finish_reason="stop")
                        return
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta", {}) or {}
                    usage = chunk.get("usage") or {}
                    yield StreamChunk(
                        delta=delta.get("content") or "",
                        done=bool(choice.get("finish_reason")),
                        tool_calls=_parse_tool_calls(delta.get("tool_calls")),
                        model=chunk.get("model", self._model(model)),
                        provider=self.name,
                        finish_reason=choice.get("finish_reason") or "",
                        input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                        output_tokens=int(usage.get("completion_tokens", 0) or 0),
                    )

    async def embed(self, texts: Iterable[str], *, model: str = "") -> list[list[float]]:
        items = list(texts)
        payload = {"model": model or "text-embedding-3-small", "input": items}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self._url("/v1/embeddings"), headers=self._headers(), json=payload)
            response.raise_for_status()
            data = response.json()
        return [item.get("embedding", []) for item in data.get("data", [])]


def _parse_tool_calls(raw: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in raw or []:
        function = item.get("function", {}) if isinstance(item, dict) else {}
        arguments_raw = function.get("arguments", "") or ""
        try:
            arguments = json.loads(arguments_raw) if arguments_raw else {}
        except json.JSONDecodeError:
            arguments = {}
        calls.append(
            ToolCall(
                id=item.get("id", ""), name=function.get("name", ""),
                arguments=arguments if isinstance(arguments, dict) else {},
                raw_arguments=arguments_raw if isinstance(arguments_raw, str) else json.dumps(arguments_raw),
            )
        )
    return calls


def _known_for(model_id: str) -> dict[str, Any]:
    lowered = model_id.lower()
    for known, info in KNOWN_MODELS.items():
        if known in lowered:
            return info
    if any(tag in lowered for tag in ("vision", "vl", "llava", "gemma3")):
        return {"vision": True, "quality": 0.6}
    if any(tag in lowered for tag in ("coder", "code")):
        return {"quality": 0.7}
    return {}
