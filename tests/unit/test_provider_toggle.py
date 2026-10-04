"""Enabling providers has to work on a fresh install.

A provider that was never enabled has no adapter yet, so "enable it" cannot mean "find the adapter
and flip a flag" - that is a 404 on a brand-new installation, which is exactly when the owner needs
it. These tests pin the behaviour: the configured catalogue is authoritative, enabling registers the
adapter, disabling leaves it inert but visible, and an unknown name is a clean NotFoundError.
"""

from __future__ import annotations

from typing import Any

import pytest

from natasha.brain import ProviderRegistry
from natasha.brain.adapters.base import ProviderAdapter
from natasha.core import NotFoundError
from natasha.core.config import ProviderSettings, Settings


class _OfflineAdapter(ProviderAdapter):
    """A provider adapter that answers locally, so the toggle can be tested without a network."""

    supports_tools = False
    supports_streaming = False

    def __init__(self, name: str, settings: Any) -> None:
        super().__init__(settings)
        self.name = name
        self.default_model = str(getattr(settings, "default_model", "") or "offline-1")

    async def list_models(self) -> list[Any]:
        return []

    async def chat(self, messages: list[Any], **kwargs: Any) -> Any:  # pragma: no cover - stub
        raise NotImplementedError


class _FakeRegistry(ProviderRegistry):
    """A registry whose adapters do not touch the network."""

    def register_provider(self, name, provider_settings=None, *, adapter=None):  # type: ignore[override]
        if adapter is None:
            adapter = _OfflineAdapter(name, provider_settings)
        return super().register_provider(name, provider_settings, adapter=adapter)


@pytest.fixture()
def registry(home):
    settings = Settings()
    settings.providers = {
        "ollama": ProviderSettings(base_url="http://127.0.0.1:11434", local=True,
                                   privacy_tier="local", default_model="llama3"),
        "openai": ProviderSettings(base_url="https://api.openai.com/v1",
                                   credential_ref="credential://openai"),
    }
    return _FakeRegistry(settings)


def test_a_disabled_provider_can_be_enabled_on_a_fresh_install(registry):
    assert not registry.has("openai"), "nothing is registered until it is enabled"
    result = registry.set_enabled("openai", True)
    assert result["enabled"] is True and result["registered"] is True
    assert registry.has("openai")
    assert registry.get("openai").enabled is True
    assert result["settings"]["base_url"] == "https://api.openai.com/v1"


def test_disabling_keeps_the_adapter_but_makes_it_inert(registry):
    registry.set_enabled("ollama", True)
    assert registry.get("ollama").enabled is True
    registry.set_enabled("ollama", False)
    assert registry.get("ollama").enabled is False
    assert "ollama" in registry.names(), "the console must still be able to show what is configured"
    assert [adapter.name for adapter in registry.enabled()] == [], "an inert provider is never routed to"


def test_the_toggle_writes_through_to_the_settings_object(registry):
    registry.set_enabled("openai", True)
    assert registry.settings.providers["openai"].enabled is True
    registry.set_enabled("openai", False)
    assert registry.settings.providers["openai"].enabled is False


def test_an_unknown_provider_is_a_not_found_not_a_crash(registry):
    with pytest.raises(NotFoundError):
        registry.set_enabled("nope", True)


def test_enabling_clears_a_stale_health_verdict(registry):
    registry.set_enabled("openai", True)
    registry._health["openai"] = "stale"  # type: ignore[assignment]
    registry.set_enabled("openai", True)
    assert "openai" not in registry._health
