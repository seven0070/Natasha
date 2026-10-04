"""Google Gemini adapter (generateContent / streamGenerateContent / embedContent)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any, AsyncIterator, Iterable

import httpx

from .base import ChatMessage, ChatResponse, ModelDescriptor, ProviderAdapter, StreamChunk

KNOWN = {
    "gemini-2.0-flash": {"context": 1_000_000, "quality": 0.82},
    "gemini-1.5-pro": {"context": 2_000_000, "quality": 0.88},
    "gemini-1.5-flash": {"context": 1_000_000, "quality": 0.72},
}


class GeminiAdapter(ProviderAdapter):
    name = "gemini"
    supports_vision = True
    supports_tools = True
    supports_embeddings = True
    default_model = "gemini-2.0-flash"
    models = ["gemini-2.0-flash", "gemini-1.5-pro"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.default_model = str(getattr(self.settings, "default_model", "") or self.default_model)

    def _api_key(self) -> str:
        if self.credential_provider is None or not self.credential_ref:
            raise ValueError("gemini requires a stored credential")
        secret = self.credential_provider(self.credential_ref, purpose="provider:gemini")
        return str(secret.get("value") or secret.get("api_key") or "")

    def _url(self, path: str, **query: str) -> str:
        params = {"key": self._api_key(), **query}
        encoded = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.base_url.rstrip('/')}{path}?{encoded}"

    def _contents(self, messages: list[ChatMessage]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        system = None
        contents: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                system = {"parts": [{"text": message.content}]}
                continue
            role = "model" if message.role == "assistant" else "user"
            parts: list[dict[str, Any]] = [{"text": message.content}]
            for image in message.images:
                if image.startswith("data:"):
                    header, _, payload = image.partition(",")
                    mime = header.split(";")[0].replace("data:", "") or "image/png"
                    parts.append({"inline_data": {"mime_type": mime, "data": payload}})
            contents.append({"role": role, "parts": parts})
        return system, contents

    async def list_models(self) -> list[ModelDescriptor]:
        try:
            async with httpx.AsyncClient(timeout=min(self.timeout, 30.0)) as client:
                response = await client.get(self._url("/v1beta/models"))
                response.raise_for_status()
                data = response.json().get("models", [])
        except Exception:
            data = []
        descriptors = [
            ModelDescriptor(
                id=item.get("name", "").split("/")[-1], provider=self.name,
                context_window=int(item.get("inputTokenLimit", 32_000) or 32_000),
                supports_vision="vision" in str(item.get("supportedGenerationMethods", [])).lower() or True,
                supports_tools=True, quality=_quality(item.get("name", "")),
            )
            for item in data if item.get("name")
        ]
        return descriptors or [ModelDescriptor(id=model, provider=self.name, context_window=1_000_000) for model in self.models]

    async def chat(self, messages: list[ChatMessage], *, model: str = "", temperature: float = 0.7,
                   max_tokens: int | None = None, tools: list[dict[str, Any]] | None = None, **params: Any) -> ChatResponse:
        system, contents = self._contents(messages)
        payload: dict[str, Any] = {"contents": contents, "generationConfig": {"temperature": temperature}}
        if max_tokens:
            payload["generationConfig"]["maxOutputTokens"] = max_tokens
        if system:
            payload["systemInstruction"] = system
        if tools:
            payload["tools"] = [{"functionDeclarations": [
                {"name": tool["function"]["name"], "description": tool["function"].get("description", ""),
                 "parameters": tool["function"].get("parameters", {})} for tool in tools if "function" in tool
            ]}]
        target = model or self.default_model
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self._url(f"/v1beta/models/{target}:generateContent"), json=payload)
            response.raise_for_status()
            data = response.json()
        latency = (time.perf_counter() - started) * 1000
        candidate = (data.get("candidates") or [{}])[0]
        text = "".join(part.get("text", "") for part in candidate.get("content", {}).get("parts", []))
        usage = data.get("usageMetadata", {}) or {}
        return ChatResponse(
            content=text, model=target, provider=self.name, finish_reason=candidate.get("finishReason", "stop").lower(),
            input_tokens=int(usage.get("promptTokenCount", 0) or 0),
            output_tokens=int(usage.get("candidatesTokenCount", 0) or 0), latency_ms=latency, raw=data,
        )

    async def stream_chat(self, messages: list[ChatMessage], *, model: str = "", temperature: float = 0.7,
                          max_tokens: int | None = None, **params: Any) -> AsyncIterator[StreamChunk]:
        system, contents = self._contents(messages)
        payload: dict[str, Any] = {"contents": contents, "generationConfig": {"temperature": temperature}}
        if system:
            payload["systemInstruction"] = system
        target = model or self.default_model
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            url = self._url(f"/v1beta/models/{target}:streamGenerateContent", alt="sse")
            async with client.stream("POST", url, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    for candidate in event.get("candidates", []):
                        for part in candidate.get("content", {}).get("parts", []):
                            if part.get("text"):
                                yield StreamChunk(delta=part["text"], model=target, provider=self.name)
                        if candidate.get("finishReason"):
                            yield StreamChunk(done=True, model=target, provider=self.name,
                                              finish_reason=candidate["finishReason"].lower())

    async def embed(self, texts: Iterable[str], *, model: str = "") -> list[list[float]]:
        target = model or "text-embedding-004"
        payload = {"requests": [{"model": f"models/{target}", "content": {"parts": [{"text": text}]}} for text in texts]}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self._url(f"/v1beta/models/{target}:batchEmbedContents"), json=payload)
            response.raise_for_status()
            data = response.json()
        return [item.get("values", []) for item in data.get("embeddings", [])]


def _quality(model_id: str) -> float:
    lowered = model_id.lower()
    for known, info in KNOWN.items():
        if known in lowered:
            return float(info["quality"])
    return 0.7 if "flash" in lowered else 0.8
