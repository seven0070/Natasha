"""Core primitives: ids, clock, hashing, risk, paths, config layering."""

from __future__ import annotations

import json

import pytest


def test_ids_are_unique_and_prefixed():
    from natasha.core import new_id, short_id

    first, second = new_id("tst"), new_id("tst")
    assert first != second and first.startswith("tst_")
    assert short_id("abcdef123456") == "abcdef12"


def test_clock_roundtrip_is_timezone_aware():
    from natasha.core import iso, parse_iso

    stamp = iso()
    parsed = parse_iso(stamp)
    assert parsed.tzinfo is not None
    assert iso(parsed) == stamp


def test_hashing_is_stable_and_canonical():
    from natasha.core import canonical_json, sha256_json, sha256_text

    assert sha256_text("abc") == sha256_text("abc")
    assert sha256_text("abc") != sha256_text("abd")
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert sha256_json({"a": 2, "b": 1}) == sha256_json({"b": 1, "a": 2})


def test_risk_ordering_and_owner_gate():
    from natasha.core.risk import RiskLevel, max_risk

    assert RiskLevel.NONE < RiskLevel.LOW < RiskLevel.MEDIUM < RiskLevel.HIGH < RiskLevel.CRITICAL
    assert max_risk(RiskLevel.LOW, RiskLevel.CRITICAL) is RiskLevel.CRITICAL
    assert RiskLevel.HIGH.requires_owner_approval() and RiskLevel.CRITICAL.requires_owner_approval()
    assert not RiskLevel.LOW.requires_owner_approval()


def test_risk_parse_accepts_names_and_numbers():
    from natasha.core.risk import RiskLevel

    assert RiskLevel.parse("critical") is RiskLevel.CRITICAL
    assert RiskLevel.parse(3) is RiskLevel.HIGH
    assert RiskLevel.parse(RiskLevel.LOW) is RiskLevel.LOW


def test_paths_live_under_the_configured_home(home):
    from natasha.core import get_paths

    paths = get_paths().ensure()
    assert paths.home == home.resolve()
    for name in ("db", "artifacts", "vault", "workspace", "reports"):
        assert getattr(paths, name).is_dir(), name
    assert str(paths.db_path("x.db")).endswith("x.db")


def test_settings_layers_defaults_then_file_then_env(home):
    from natasha.core import load_settings

    config = home / "config" / "natasha.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[brain]\nprefer_local = false\n', encoding="utf-8")

    settings = load_settings(str(home))
    assert settings.brain.prefer_local is False

    settings = load_settings(str(home), env={"NATASHA_BRAIN__PREFER_LOCAL": "true",
                                             "NATASHA_LOG_LEVEL": "DEBUG"})
    assert settings.brain.prefer_local is True
    assert settings.log_level == "DEBUG"

    settings = load_settings(str(home), overrides={"log_level": "WARNING"})
    assert settings.log_level == "WARNING"


def test_settings_reject_malformed_toml(home):
    from natasha.core import ConfigurationError, load_settings

    config = home / "config" / "natasha.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("this is not toml = = =", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_settings(str(home))


def test_provider_matrix_covers_the_documented_providers():
    from natasha.core.config import _default_provider_matrix

    providers = _default_provider_matrix()
    for expected in ("ollama", "llamacpp", "lmstudio", "vllm", "openai", "anthropic", "gemini", "groq",
                     "cerebras", "mistral", "xai", "openrouter", "nvidia", "huggingface"):
        assert expected in providers, expected
    assert providers["ollama"].local is True
    assert providers["openai"].credential_ref == "credential://openai"


def test_settings_save_and_reload(home):
    from natasha.core import load_settings

    settings = load_settings(str(home))
    settings.profile = "test-profile"
    path = settings.save()
    assert path.is_file()
    assert load_settings(str(home)).profile == "test-profile"
