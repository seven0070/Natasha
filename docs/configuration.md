# Configuration

Natasha is configured with TOML plus a small number of environment variables. Settings are loaded
in a fixed order, and the parts that can change at runtime are deliberately narrower than the parts
that cannot.

## Precedence

Lowest to highest:

1. built-in defaults (`natasha.core.config.Settings`)
2. `config/natasha.toml` in the repository (if present)
3. `$NATASHA_HOME/config/natasha.toml`
4. environment variables
5. explicit `overrides=` passed by code (tests, embedding)

Malformed TOML raises `ConfigurationError` at load time — the process refuses to start with a broken
configuration instead of silently using defaults.

## The home directory

| Variable | Meaning |
| --- | --- |
| `NATASHA_HOME` | where every piece of state lives (default `~/.natasha`) |
| `NATASHA_*` | settings override, e.g. `NATASHA_PORT=9000`, `NATASHA_BRAIN__PREFER_LOCAL=false` |

Nested keys use a double underscore: `NATASHA_VOICE__SPEED=190` sets `voice.speed`.
**Secrets are excluded by design**: an environment variable whose first path segment is `api_key`,
`secret` or `token` is ignored, so a credential cannot be smuggled in through the environment. Keys
belong in the encrypted vault (`natasha credentials add`, or the console's Providers screen).

## Settings reference

Top-level keys, with the defaults that ship:

| Key | Default | Notes |
| --- | --- | --- |
| `profile` | `local-first` | informational: how the defaults were chosen |
| `deployment` | `desktop` | `desktop` \| `server` \| `container` |
| `host`, `port` | `127.0.0.1`, `8000` | bind address for `natasha serve` |
| `log_level` | `INFO` | standard logging levels |
| `data_dir` | `''` | override the data directory inside the home |
| `owner_id` | `''` | set when the owner is created |
| `auth_required` | `true` | **do not disable in production** |
| `session_ttl_minutes` | `720` | session lifetime |
| `providers` | catalogue of 17 | see below |
| `security` | see [security.md](security.md) | default-deny policy |
| `memory` | retrieval weights | `retrieval_k`, `weights`, `decay_half_life_days`, `embed_dim` |
| `brain` | routing | `prefer_local`, `default_provider`, `fallback_depth`, `routing_weights`, `stream` |
| `executive` | the loop's limits | `max_steps`, `max_repairs`, `require_verification`, `allow_unverified_success` |
| `creation` | generators | `image_endpoint`, `image_model`, `tts_endpoint`, `video_*` |
| `voice` | speech | `speed`, `language`, `wake_phrases`, `auto_speak_replies`, `tts_endpoint` |
| `features` | feature switches | `affect`, `computer`, `creation`, `evolution`, `marketplace`, `mcp`, `skills`, `vision`, `voice` |
| `limits` | API rate limits | `enabled`, `multiplier`, `overrides` (see [api.md](api.md#rate-limits)) |

### Providers

Every provider is **disabled until configured**. The catalogue ships with the right base URLs:

| Provider | Base URL | Kind |
| --- | --- | --- |
| `ollama` | `http://127.0.0.1:11434` | local |
| `llamacpp` | `http://127.0.0.1:8080` | local (llama.cpp server, GGUF) |
| `lmstudio` | `http://127.0.0.1:1234/v1` | local |
| `vllm` | `http://127.0.0.1:8001/v1` | local (private cloud tier) |
| `echo` | — | offline placeholder; never wins routing while a real model is healthy |
| `openai`, `anthropic`, `gemini` | vendor endpoints | cloud |
| `groq`, `cerebras`, `mistral`, `xai`, `nvidia`, `huggingface` | vendor endpoints | cloud |
| `openrouter` | aggregator | cloud |
| `openai_compatible`, `custom` | **any** OpenAI-compatible server you name | wherever you point it |

Each entry takes `enabled`, `base_url`, `credential_ref`, `default_model`, `models`,
`timeout_seconds`, `max_retries`, `local`, `privacy_tier`, `cost_per_1k_in`, `cost_per_1k_out`.

### Security posture

`security.default_effect` is `deny`: a capability that is not explicitly allowed is refused. The
lists (`fs_read_allow`, `fs_write_allow`, `fs_deny_globs`, `command_allowlist`,
`command_denylist`) accept the placeholders `$NATASHA_HOME`, `$HOME`, `$WORKSPACE`, `$ARTIFACTS`,
`$DATA`, `$UPLOADS`, which are resolved against the real paths at load time. Details and the threat
model: [security.md](security.md).

### Executive limits

`executive.max_steps` bounds a single turn's tool steps, `max_repairs` bounds the repair ladder, and
`require_verification: true` with `allow_unverified_success: false` is what stops a turn from
reporting success it cannot back. Lowering these makes Natasha stricter, not looser.

## Where the file is written

```bash
# API (owner only)
POST /api/settings/save         # writes $NATASHA_HOME/config/natasha.toml
PATCH /api/settings             # updates the mutable subset in memory
```

`PATCH /api/settings` accepts only `MUTABLE` keys. Governance, security roots and the constitution
are **not** mutable through that endpoint: they change through the constitutional process
(`/api/governance/*`), which requires explicit owner approval. That split is intentional — a
settings endpoint is not a governance bypass.

## Runtime mutability

| Change | How | Takes effect |
| --- | --- | --- |
| Provider enabled/disabled | `natasha providers enable/disable`, console | immediately, and saved |
| Provider credential | `natasha credentials add/rotate` | immediately (broker reads the vault) |
| Routing preference | `PATCH /api/providers/routing` | next routing decision |
| Log level, voice speed, feature switches | `PATCH /api/settings` | next use of that subsystem |
| Security posture, policy lists | edit TOML, restart | restart |
| Constitution, identity, protected invariants | `/api/governance/*` + owner approval | governed change |

## A minimal production file

```toml
# $NATASHA_HOME/config/natasha.toml
deployment = "server"
host = "0.0.0.0"
port = 8000
log_level = "INFO"
auth_required = true

[brain]
prefer_local = true
fallback_depth = 3

[executive]
max_steps = 24
require_verification = true
allow_unverified_success = false

[providers.ollama]
enabled = true
base_url = "http://127.0.0.1:11434"
local = true
default_model = "llama3.1:8b"

[voice]
speed = 175
auto_speak_replies = false
```

Regenerate the file from the running defaults with `POST /api/settings/save` or
`python3 -c "from natasha.core import load_settings; print(load_settings().save())"`.
