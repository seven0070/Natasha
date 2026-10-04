"""Provider adapters."""

from .anthropic import AnthropicAdapter
from .base import ChatMessage, ChatResponse, ProviderAdapter, StreamChunk, ToolCall
from .echo import EchoAdapter
from .gemini import GeminiAdapter
from .ollama import OllamaAdapter
from .openai_compatible import OpenAICompatibleAdapter, OPENAI_COMPATIBLE_PROVIDERS

__all__ = [
    "AnthropicAdapter", "ChatMessage", "ChatResponse", "ProviderAdapter", "StreamChunk", "ToolCall",
    "EchoAdapter", "GeminiAdapter", "OllamaAdapter", "OpenAICompatibleAdapter", "OPENAI_COMPATIBLE_PROVIDERS",
]
