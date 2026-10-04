"""The brain: provider-independent model access.

Adapters normalise every provider to one interface; the registry tracks health and discovers
models; the router picks a model from task, modality, quality, latency, cost, privacy, hardware,
context window and availability - preferring local models when configured. Fallback runs down a
chain so a provider outage degrades rather than fails.
"""

from typing import Any

from .adapters.base import ChatMessage, ChatResponse, ProviderAdapter, StreamChunk, ToolCall
from .adapters.echo import EchoAdapter
from .client import BrainClient, CompletionRequest, get_brain
from .models import Capability, ModelInfo, ModelRegistry, get_model_registry
from .registry import ProviderRegistry, ProviderStatus, get_provider_registry
from .router import RouteExplanation, Router, RoutingDecision, TaskProfile
from .usage import UsageRecord, UsageTracker, get_usage_tracker

def get_brain_client(**kwargs: Any) -> BrainClient:
    """Alias used by the perception/voice engines."""
    return get_brain(**kwargs)


__all__ = [
    "ChatMessage", "ChatResponse", "ProviderAdapter", "StreamChunk", "ToolCall", "EchoAdapter",
    "BrainClient", "CompletionRequest", "get_brain", "Capability", "ModelInfo", "ModelRegistry",
    "get_model_registry", "ProviderRegistry", "ProviderStatus", "get_provider_registry",
    "RouteExplanation", "Router", "RoutingDecision", "TaskProfile", "UsageRecord", "UsageTracker",
    "get_usage_tracker", "get_brain_client",
]
