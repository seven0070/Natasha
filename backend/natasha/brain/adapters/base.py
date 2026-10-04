"""Provider adapter interface.

Every provider - cloud or local - is reduced to the same handful of capabilities here. Adapters
return *normalised* results, so routing, fallback, usage accounting and the UI never need to know
which vendor answered.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable


@dataclass
class ChatMessage:
    role: str
    content: str
    name: str = ""
    images: list[str] = field(default_factory=list)   # data URLs or local paths
    tool_calls: list["ToolCall"] = field(default_factory=list)
    tool_call_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            data["name"] = self.name
        if self.tool_calls:
            data["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        return data

    @classmethod
    def system(cls, content: str) -> "ChatMessage":
        return cls("system", content)

    @classmethod
    def user(cls, content: str, *, images: list[str] | None = None) -> "ChatMessage":
        return cls("user", content, images=images or [])

    @classmethod
    def assistant(cls, content: str) -> "ChatMessage":
        return cls("assistant", content)

    @classmethod
    def tool(cls, content: str, *, tool_call_id: str, name: str = "") -> "ChatMessage":
        return cls("tool", content, tool_call_id=tool_call_id, name=name)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": self.raw_arguments or "{}"}}


@dataclass
class ChatResponse:
    content: str
    model: str
    provider: str
    finish_reason: str = "stop"
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)
    refused: bool = False
    refusal_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content, "model": self.model, "provider": self.provider,
            "finish_reason": self.finish_reason, "tool_calls": [call.to_dict() for call in self.tool_calls],
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms, "cost_usd": self.cost_usd,
        }


@dataclass
class StreamChunk:
    """One streamed delta. ``done`` marks the final chunk."""

    delta: str = ""
    done: bool = False
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    finish_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ModelDescriptor:
    """A model advertised by a provider."""

    id: str
    provider: str
    context_window: int = 8192
    supports_vision: bool = False
    supports_tools: bool = False
    supports_streaming: bool = True
    supports_embeddings: bool = False
    supports_audio: bool = False
    local: bool = False
    quality: float = 0.5
    cost_per_1k_in: float = 0.0
    cost_per_1k_out: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(abc.ABC):
    """Base class for provider adapters."""

    #: registry name, e.g. ``openai``
    name: str = "base"
    #: whether this provider runs on the local machine
    local: bool = False
    #: privacy tier: local | private_cloud | cloud
    privacy_tier: str = "cloud"
    #: rough capability flags used by the router before model discovery
    supports_tools: bool = True
    supports_vision: bool = False
    supports_embeddings: bool = False
    supports_streaming: bool = True
    default_model: str = ""
    models: list[str] = []

    def __init__(self, settings: Any = None, *, credential_provider: Any = None, timeout: float = 120.0) -> None:
        self.settings = settings
        self.credential_provider = credential_provider
        self.timeout = timeout

    # -- configuration --------------------------------------------------------- #
    @property
    def base_url(self) -> str:
        return str(getattr(self.settings, "base_url", "") or "")

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "enabled", False))

    @property
    def credential_ref(self) -> str:
        return str(getattr(self.settings, "credential_ref", "") or "")

    @property
    def configured_models(self) -> list[str]:
        configured = list(getattr(self.settings, "models", []) or [])
        if configured:
            return configured
        default = str(getattr(self.settings, "default_model", "") or self.default_model)
        return [default] if default else list(self.models)

    def _headers(self, *, include_auth: bool = True) -> dict[str, str]:
        """Auth headers, obtained through the credential broker when configured."""
        headers = {"Content-Type": "application/json"}
        if include_auth and self.credential_provider is not None and self.credential_ref:
            try:
                headers.update(self.credential_provider(self.credential_ref, purpose=f"provider:{self.name}"))
            except Exception:
                # No credential available: the request will fail with a provider error that says so.
                pass
        return headers

    # -- capability discovery --------------------------------------------------- #
    @abc.abstractmethod
    async def list_models(self) -> list[ModelDescriptor]:
        """Discover available models."""

    @abc.abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **params: Any,
    ) -> ChatResponse:
        """One-shot completion."""

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **params: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming completion. Adapters that cannot stream fall back to one chunk."""
        response = await self.chat(messages, model=model, temperature=temperature, max_tokens=max_tokens, **params)
        yield StreamChunk(delta=response.content, done=True, model=response.model, provider=response.provider,
                          finish_reason=response.finish_reason, input_tokens=response.input_tokens,
                          output_tokens=response.output_tokens)

    async def embed(self, texts: Iterable[str], *, model: str = "") -> list[list[float]]:
        raise NotImplementedError(f"{self.name} does not support embeddings")

    async def health(self) -> tuple[bool, str]:
        """Cheap liveness probe."""
        try:
            models = await self.list_models()
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, f"{len(models)} models available"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "local": self.local, "privacy_tier": self.privacy_tier,
            "base_url": self.base_url, "enabled": self.enabled, "default_model": self.default_model,
            "models": self.configured_models,
            "capabilities": {
                "tools": self.supports_tools, "vision": self.supports_vision,
                "embeddings": self.supports_embeddings, "streaming": self.supports_streaming,
            },
        }
