"""Ollama adapter (local models) plus llama.cpp / GGUF server support.

Ollama serves ``/api/chat``, ``/api/tags`` and ``/api/embeddings``. llama.cpp's server exposes an
OpenAI-compatible ``/v1`` surface, so GGUF models loaded there are handled by
:class:`OpenAICompatibleAdapter` - this module covers the native Ollama API and is also used to
detect locally installed models for the local-model preference in routing.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Iterable

import httpx

from .base import ChatMessage, ChatResponse, ModelDescriptor, ProviderAdapter, StreamChunk, ToolCall


class OllamaAdapter(ProviderAdapter):
    name = "ollama"
    local = True
    privacy_tier = "local"
    supports_tools = True
    supports_embeddings = True
    default_model = "llama3.2"
    models: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.default_model = str(getattr(self.settings, "default_model", "") or self.default_model)

    def _base(self) -> str:
        return (self.base_url or "http://127.0.0.1:11434").rstrip("/")

    async def list_models(self) -> list[ModelDescriptor]:
        async with httpx.AsyncClient(timeout=min(self.timeout, 15.0)) as client:
            response = await client.get(f"{self._base()}/api/tags")
            response.raise_for_status()
            data = response.json().get("models", [])
        descriptors: list[ModelDescriptor] = []
        for item in data:
            name = item.get("name") or item.get("model") or ""
            if not name:
                continue
            details = item.get("details", {}) or {}
            family = str(details.get("family", "")).lower()
            descriptors.append(
                ModelDescriptor(
                    id=name, provider=self.name, local=True,
                    context_window=_context_for(item),
                    supports_vision=any(tag in name.lower() for tag in ("llava", "vision", "vl", "gemma3", "minicpm-v")),
                    supports_tools=not family.startswith("llava"),
                    supports_embeddings=any(tag in name.lower() for tag in ("embed", "bge", "nomic")),
                    quality=_quality_for(name),
                    metadata={"size": item.get("size"), "family": family, "parameters": details.get("parameter_size", "")},
                )
            )
        return descriptors

    async def chat(self, messages: list[ChatMessage], *, model: str = "", temperature: float = 0.7,
                   max_tokens: int | None = None, tools: list[dict[str, Any]] | None = None, **params: Any) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": [
                {"role": message.role, "content": message.content}
                | ({"images": [image.split(",", 1)[-1] for image in message.images]} if message.images else {})
                | ({"tool_calls": [call.to_dict() for call in message.tool_calls]} if message.tool_calls else {})
                for message in messages
            ],
            "stream": False,
            "options": {"temperature": temperature} | ({"num_predict": max_tokens} if max_tokens else {}),
        }
        if tools:
            payload["tools"] = tools
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self._base()}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        latency = (time.perf_counter() - started) * 1000
        message = data.get("message", {}) or {}
        return ChatResponse(
            content=message.get("content") or "",
            model=data.get("model", model or self.default_model), provider=self.name,
            finish_reason=data.get("done_reason", "stop"),
            tool_calls=[
                ToolCall(id=f"ollama-{index}", name=call.get("function", {}).get("name", ""),
                         arguments=call.get("function", {}).get("arguments", {}) or {})
                for index, call in enumerate(message.get("tool_calls") or [])
            ],
            input_tokens=int(data.get("prompt_eval_count", 0) or 0),
            output_tokens=int(data.get("eval_count", 0) or 0),
            latency_ms=latency, raw=data,
        )

    async def stream_chat(self, messages: list[ChatMessage], *, model: str = "", temperature: float = 0.7,
                          max_tokens: int | None = None, **params: Any) -> AsyncIterator[StreamChunk]:
        payload = {
            "model": model or self.default_model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "stream": True,
            "options": {"temperature": temperature},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", f"{self._base()}/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = chunk.get("message", {}) or {}
                    yield StreamChunk(
                        delta=message.get("content", "") or "",
                        done=bool(chunk.get("done")),
                        model=chunk.get("model", model or self.default_model), provider=self.name,
                        finish_reason=chunk.get("done_reason", "") if chunk.get("done") else "",
                        input_tokens=int(chunk.get("prompt_eval_count", 0) or 0),
                        output_tokens=int(chunk.get("eval_count", 0) or 0),
                    )

    async def embed(self, texts: Iterable[str], *, model: str = "") -> list[list[float]]:
        target = model or "nomic-embed-text"
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for text in texts:
                response = await client.post(f"{self._base()}/api/embeddings", json={"model": target, "prompt": text})
                response.raise_for_status()
                vectors.append(response.json().get("embedding", []))
        return vectors


def _context_for(item: dict[str, Any]) -> int:
    details = item.get("details", {}) or {}
    parameters = str(details.get("parameter_size", "")).lower()
    if "70b" in parameters or "72b" in parameters:
        return 32_768
    if any(size in parameters for size in ("13b", "14b", "27b", "32b")):
        return 16_384
    return 8_192


def _quality_for(name: str) -> float:
    lowered = name.lower()
    if any(size in lowered for size in ("70b", "72b", "qwq", "405b")):
        return 0.82
    if any(size in lowered for size in ("27b", "32b", "34b", "30b")):
        return 0.74
    if any(size in lowered for size in ("13b", "14b")):
        return 0.66
    if any(size in lowered for size in ("7b", "8b", "9b")):
        return 0.58
    if any(size in lowered for size in ("3b", "4b")):
        return 0.45
    return 0.55
