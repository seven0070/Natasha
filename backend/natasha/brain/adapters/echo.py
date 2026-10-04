"""Offline echo provider.

A deterministic, network-free adapter used by tests, the `doctor` command and demos. It is a real
provider (it satisfies the adapter contract) but it does not reason: it echoes the last user
message and reports honest usage numbers, so end-to-end flows can be exercised without API keys.

It is never selected automatically unless it is the only enabled provider.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Iterable

from .base import ChatMessage, ChatResponse, ModelDescriptor, ProviderAdapter, StreamChunk


class EchoAdapter(ProviderAdapter):
    name = "echo"
    local = True
    privacy_tier = "local"
    supports_tools = False
    supports_vision = False
    supports_embeddings = True
    supports_streaming = True
    default_model = "echo-1"
    models = ["echo-1"]

    async def list_models(self) -> list[ModelDescriptor]:
        return [
            ModelDescriptor(
                id="echo-1", provider=self.name, local=True, quality=0.1,
                context_window=32_768, supports_embeddings=True,
                metadata={"note": "offline echo provider for tests and demos"},
            )
        ]

    async def chat(self, messages: list[ChatMessage], *, model: str = "", temperature: float = 0.7,
                   max_tokens: int | None = None, tools: list[dict[str, Any]] | None = None, **params: Any) -> ChatResponse:
        last_user = next((message.content for message in reversed(messages) if message.role == "user"), "")
        system_turns = sum(1 for message in messages if message.role == "system")
        content = (
            f"[echo] Received {len(messages)} message(s) ({system_turns} system). "
            f"Latest instruction: {last_user[:400]}"
        )
        return ChatResponse(
            content=content, model=model or self.default_model, provider=self.name, finish_reason="stop",
            input_tokens=sum(len(message.content.split()) for message in messages),
            output_tokens=len(content.split()), latency_ms=1.0,
        )

    async def stream_chat(self, messages: list[ChatMessage], *, model: str = "", temperature: float = 0.7,
                          max_tokens: int | None = None, **params: Any) -> AsyncIterator[StreamChunk]:
        response = await self.chat(messages, model=model, temperature=temperature, max_tokens=max_tokens)
        for index, word in enumerate(response.content.split(" ")):
            await asyncio.sleep(0)
            yield StreamChunk(delta=("" if index == 0 else " ") + word, model=response.model, provider=self.name)
        yield StreamChunk(done=True, model=response.model, provider=self.name, finish_reason="stop",
                          input_tokens=response.input_tokens, output_tokens=response.output_tokens)

    async def embed(self, texts: Iterable[str], *, model: str = "") -> list[list[float]]:
        from ... import memory as _memory  # local import keeps the adapter dependency-light

        return [list(_memory.HashingEmbedder().embed(text)) for text in texts]
