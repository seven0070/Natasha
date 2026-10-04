"""Layered configuration.

Precedence (lowest to highest): built-in defaults -> ``config/natasha.toml`` in the repo ->
``$NATASHA_HOME/config/natasha.toml`` -> environment variables -> explicit overrides.

Secrets are never stored here; only *references* (e.g. ``credential://openai``) are.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .paths import NatashaPaths, build_paths

ENV_PREFIX = "NATASHA_"


@dataclass
class ProviderSettings:
    """Per-provider configuration. Credentials live in the credential vault."""

    enabled: bool = False
    base_url: str = ""
    credential_ref: str = ""
    default_model: str = ""
    models: list[str] = field(default_factory=list)
    timeout_seconds: float = 120.0
    max_retries: int = 2
    local: bool = False
    privacy_tier: str = "cloud"  # local | private_cloud | cloud
    cost_per_1k_in: float = 0.0
    cost_per_1k_out: float = 0.0


@dataclass
class LimitSettings:
    """API rate limits. Guards against runaway loops and passphrase guessing."""

    enabled: bool = True
    multiplier: float = 1.0            # >1 loosens every bucket, <1 tightens it
    overrides: dict[str, Any] = field(default_factory=dict)   # bucket -> [per_minute, burst]


@dataclass
class SecuritySettings:
    """Security posture. Defaults are deliberately restrictive."""

    default_effect: str = "deny"  # deny | allow
    require_approval_for: list[str] = field(
        default_factory=lambda: ["fs.write_outside_workspace", "shell.exec", "computer.control", "credential.use", "code.exec"]
    )
    fs_read_allow: list[str] = field(
        default_factory=lambda: ["$NATASHA_HOME", "$HOME", "$WORKSPACE", "$ARTIFACTS", "$DATA", "$UPLOADS"]
    )
    fs_write_allow: list[str] = field(default_factory=lambda: ["$WORKSPACE", "$ARTIFACTS", "$UPLOADS"])
    fs_deny_globs: list[str] = field(
        default_factory=lambda: [
            "**/.ssh/**", "**/.aws/**", "**/.gnupg/**", "**/.netrc", "**/id_rsa*", "**/id_ed25519*",
            "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/authorized_keys",
            "$NATASHA_HOME/vault/**", "$NATASHA_HOME/keys/**", "$NATASHA_HOME/db/**",
            "/etc/shadow", "/etc/sudoers", "/etc/sudoers.d/**", "/etc/ssh/**",
        ]
    )
    network_allow_domains: list[str] = field(
        default_factory=lambda: [
            "api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com", "api.groq.com",
            "api.cerebras.ai", "api.mistral.ai", "api.x.ai", "openrouter.ai", "integrate.api.nvidia.com",
            "api-inference.huggingface.co", "huggingface.co", "pypi.org", "files.pythonhosted.org",
            "registry.npmjs.org", "127.0.0.1", "localhost",
        ]
    )
    network_deny_domains: list[str] = field(default_factory=lambda: ["metadata.google.internal", "169.254.169.254"])
    max_tool_output_bytes: int = 262_144
    command_allowlist: list[str] = field(
        default_factory=lambda: ["ls", "cat", "head", "tail", "wc", "grep", "find", "pwd", "echo", "git", "python3", "python", "pytest", "ruff", "node", "npm", "pip", "unzip", "tar", "df", "du", "env", "which", "date"]
    )
    command_denylist: list[str] = field(
        default_factory=lambda: ["rm -rf /", "mkfs", ":(){", "dd if=/dev/zero", "shutdown", "reboot", "chmod 777 /", "curl | sh", "wget | sh"]
    )


@dataclass
class MemorySettings:
    db_backend: str = "sqlite"  # sqlite | postgres
    embed_dim: int = 384
    retrieval_k: int = 12
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "semantic": 0.34, "keyword": 0.22, "entity": 0.14, "temporal": 0.08,
            "recency": 0.10, "importance": 0.07, "task": 0.05,
        }
    )
    decay_half_life_days: float = 45.0


@dataclass
class BrainSettings:
    """Provider-independent brain behaviour."""

    default_provider: str = ""
    routing_weights: dict[str, float] = field(
        default_factory=lambda: {
            "quality": 0.30, "cost": 0.15, "latency": 0.15, "privacy": 0.25, "availability": 0.15,
        }
    )
    prefer_local: bool = True
    local_privacy_boost: float = 0.35
    context_window_floor: int = 8192
    stream: bool = True
    fallback_depth: int = 3
    health_ttl_seconds: float = 90.0


@dataclass
class CreationSettings:
    """Image/audio/video creation: endpoints are optional; absence degrades honestly."""

    image_endpoint: str = ""
    image_model: str = ""
    tts_endpoint: str = ""
    video_fps: int = 24
    video_resolution: str = "1280x720"
    max_images_per_job: int = 8


@dataclass
class VoiceSettings:
    """Speech output/inout preferences."""

    tts_endpoint: str = ""
    default_voice: str = ""
    speed: int = 175
    language: str = "en"
    wake_phrases: list[str] = field(default_factory=lambda: ["hey natasha", "ok natasha", "natasha"])
    wake_fuzzy: int = 1
    auto_speak_replies: bool = False


@dataclass
class ExecutiveSettings:
    max_steps: int = 40
    max_repairs: int = 3
    step_timeout_seconds: float = 300.0
    require_verification: bool = True
    allow_unverified_success: bool = False


@dataclass
class Settings:
    """Top-level, serialisable settings object."""

    profile: str = "local-first"
    deployment: str = "desktop"  # desktop | headless | server | lan
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    data_dir: str = ""
    owner_id: str = ""
    auth_required: bool = True
    session_ttl_minutes: int = 720
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    brain: BrainSettings = field(default_factory=BrainSettings)
    executive: ExecutiveSettings = field(default_factory=ExecutiveSettings)
    creation: CreationSettings = field(default_factory=CreationSettings)
    limits: LimitSettings = field(default_factory=LimitSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    features: dict[str, bool] = field(
        default_factory=lambda: {
            "voice": True, "vision": True, "computer": True, "mcp": True, "skills": True,
            "marketplace": True, "creation": True, "evolution": True, "affect": True,
        }
    )
    paths: NatashaPaths | None = None

    # -- helpers --------------------------------------------------------------- #
    def provider(self, name: str) -> ProviderSettings:
        return self.providers.setdefault(name, ProviderSettings())

    def resolved_paths(self) -> NatashaPaths:
        return self.paths or build_paths(self.data_dir or None)

    def to_dict(self, *, redact_paths: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if redact_paths or self.paths is None:
            data["paths"] = self.paths.as_dict() if self.paths else None
        else:
            data["paths"] = self.paths.as_dict()
        return data

    def save(self, path: Path | None = None) -> Path:
        target = path or (self.resolved_paths().ensure().config / "natasha.toml")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dumps_toml(self.to_dict()), encoding="utf-8")
        return target


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def _default_provider_matrix() -> dict[str, ProviderSettings]:
    """Known providers with sensible defaults. All disabled until configured."""
    local = {"local": True, "privacy_tier": "local"}
    return {
        "ollama": ProviderSettings(base_url="http://127.0.0.1:11434", **local),
        "llamacpp": ProviderSettings(base_url="http://127.0.0.1:8080", **local),
        "lmstudio": ProviderSettings(base_url="http://127.0.0.1:1234/v1", **local),
        "vllm": ProviderSettings(base_url="http://127.0.0.1:8001/v1", local=True, privacy_tier="private_cloud"),
        "openai": ProviderSettings(base_url="https://api.openai.com/v1", credential_ref="credential://openai"),
        "anthropic": ProviderSettings(base_url="https://api.anthropic.com", credential_ref="credential://anthropic"),
        "gemini": ProviderSettings(base_url="https://generativelanguage.googleapis.com", credential_ref="credential://gemini"),
        "groq": ProviderSettings(base_url="https://api.groq.com/openai/v1", credential_ref="credential://groq"),
        "cerebras": ProviderSettings(base_url="https://api.cerebras.ai/v1", credential_ref="credential://cerebras"),
        "mistral": ProviderSettings(base_url="https://api.mistral.ai/v1", credential_ref="credential://mistral"),
        "xai": ProviderSettings(base_url="https://api.x.ai/v1", credential_ref="credential://xai"),
        "openrouter": ProviderSettings(base_url="https://openrouter.ai/api/v1", credential_ref="credential://openrouter"),
        "nvidia": ProviderSettings(base_url="https://integrate.api.nvidia.com/v1", credential_ref="credential://nvidia"),
        "huggingface": ProviderSettings(base_url="https://api-inference.huggingface.co", credential_ref="credential://huggingface"),
        "openai_compatible": ProviderSettings(base_url="", credential_ref="credential://openai_compatible"),
        "custom": ProviderSettings(base_url="", credential_ref="credential://custom"),
        # Offline provider used by tests, `natasha doctor` and demos; never wins routing by default.
        "echo": ProviderSettings(enabled=False, local=True, privacy_tier="local", default_model="echo-1"),
    }


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_env(raw: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    for key, value in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX):].lower().split("__")
        if path and path[0] in {"api_key", "secret", "token"}:
            continue  # secrets belong in the vault, never in settings
        cursor: dict[str, Any] = raw
        for part in path[:-1]:
            node = cursor.get(part)
            if not isinstance(node, dict):
                node = {}
                cursor[part] = node
            cursor = node
        cursor[path[-1]] = _coerce(value)
    return raw


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"none", "null", ""}:
        return None if lowered != "" else ""
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _path_table(paths: NatashaPaths) -> dict[str, str]:
    """Placeholder -> real directory map used in security path lists."""
    return {
        "$HOME": str(Path.home()), "$WORKSPACE": str(paths.workspace), "$ARTIFACTS": str(paths.artifacts),
        "$UPLOADS": str(paths.uploads), "$NATASHA_HOME": str(paths.home), "$DATA": str(paths.data),
    }


def _replace_all(text: str, table: dict[str, str]) -> str:
    for needle, replacement in table.items():
        text = text.replace(needle, replacement)
    return text


def _config_candidates(paths: NatashaPaths, env: dict[str, str] | None = None) -> list[Path]:
    """The configuration files to layer, lowest precedence first.

    Three sources, in order:

    * the *checkout's* ``config/natasha.toml`` - found by walking up from this module to the
      directory holding ``pyproject.toml``, so a source checkout is configured by the repository;
    * ``$NATASHA_CONFIG`` - an explicit path, used by the container image (mounted at
      ``/app/config/natasha.toml``) and by operators who keep the file elsewhere;
    * ``$NATASHA_HOME/config/natasha.toml`` - the machine's own settings, written by
      :meth:`Settings.save`, and the only one that may contain machine-specific paths.

    A missing file is not an error: every layer is optional and the built-in defaults stand.
    """
    environment = env if env is not None else dict(os.environ)
    found: list[Path] = []
    for parent in Path(__file__).resolve().parents[:4]:
        candidate = parent / "config" / "natasha.toml"
        if (parent / "pyproject.toml").is_file() and candidate.is_file():
            found.append(candidate)
            break
    explicit = environment.get("NATASHA_CONFIG", "").strip()
    if explicit:
        found.append(Path(explicit).expanduser())
    found.append(paths.config / "natasha.toml")
    return found


def load_settings(
    home: str | os.PathLike[str] | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> Settings:
    """Load layered settings. Raises :class:`ConfigurationError` on malformed files."""
    paths = build_paths(home)
    raw: dict[str, Any] = {
        "providers": {name: asdict(cfg) for name, cfg in _default_provider_matrix().items()},
    }
    for candidate in _config_candidates(paths, env):
        if candidate.is_file():
            try:
                with open(candidate, "rb") as handle:
                    raw = _deep_merge(raw, tomllib.load(handle))
            except tomllib.TOMLDecodeError as exc:
                raise ConfigurationError(f"invalid TOML in {candidate}: {exc}") from exc
    raw = _apply_env(raw, env if env is not None else dict(os.environ))
    if overrides:
        raw = _deep_merge(raw, overrides)
    providers_raw = raw.pop("providers", {}) or {}
    providers = {name: ProviderSettings(**_filter_fields(ProviderSettings, cfg)) for name, cfg in providers_raw.items()}
    table = _path_table(paths)
    security = SecuritySettings(**_filter_fields(SecuritySettings, raw.pop("security", {}) or {}))
    for _key in ("fs_read_allow", "fs_write_allow", "fs_deny_globs"):
        setattr(security, _key, [_replace_all(str(_item), table) for _item in getattr(security, _key)])
    memory = MemorySettings(**_filter_fields(MemorySettings, raw.pop("memory", {}) or {}))
    brain = BrainSettings(**_filter_fields(BrainSettings, raw.pop("brain", {}) or {}))
    executive = ExecutiveSettings(**_filter_fields(ExecutiveSettings, raw.pop("executive", {}) or {}))
    creation = CreationSettings(**_filter_fields(CreationSettings, raw.pop("creation", {}) or {}))
    limits = LimitSettings(**_filter_fields(LimitSettings, raw.pop("limits", {}) or {}))
    voice = VoiceSettings(**_filter_fields(VoiceSettings, raw.pop("voice", {}) or {}))
    settings = Settings(
        providers=providers,
        security=security,
        memory=memory,
        brain=brain,
        executive=executive,
        creation=creation,
        limits=limits,
        voice=voice,
        **_filter_fields(Settings, raw),
    )
    settings.paths = paths
    if not settings.data_dir:
        settings.data_dir = str(paths.home)
    return settings


def _filter_fields(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    allowed = set(getattr(cls, "__dataclass_fields__", {}))
    return {key: value for key, value in data.items() if key in allowed}


def dumps_toml(data: dict[str, Any]) -> str:
    """Minimal, predictable TOML writer (no third-party dependency)."""

    def render(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return json.dumps(value)
        if isinstance(value, list):
            return "[" + ", ".join(render(item) for item in value) + "]"
        raise ConfigurationError(f"cannot serialise {type(value).__name__} to TOML")

    lines: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []

    def flush(prefix: str, mapping: dict[str, Any]) -> None:
        scalars = {k: v for k, v in mapping.items() if not isinstance(v, dict)}
        for key, value in scalars.items():
            if value is None:
                continue
            lines.append(f"{key} = {render(value)}")
        for key, value in mapping.items():
            if isinstance(value, dict):
                name = f"{prefix}{key}"
                tables.append((name, value))

    flush("", data)
    while tables:
        name, mapping = tables.pop(0)
        before = len(tables)
        lines.append(f"\n[{name}]")
        scalars = {k: v for k, v in mapping.items() if not isinstance(v, dict)}
        for key, value in scalars.items():
            if value is None:
                continue
            lines.append(f"{key} = {render(value)}")
        nested = [(f"{name}.{k}", v) for k, v in mapping.items() if isinstance(v, dict)]
        tables[before:before] = nested
    return "\n".join(lines) + "\n"
