# Model providers

Natasha talks to models through adapters behind one registry. The executive never contains
provider-specific logic: it asks the brain for a completion, the router chooses a model, and the
registry falls back when a provider misbehaves.

## Local vs cloud

| Tier | Meaning | Examples |
| --- | --- | --- |
| `local` | the model runs on this machine; requests never leave it | Ollama, llama.cpp, LM Studio |
| `private_cloud` | a model you host (VPS, GPU box, vLLM) | vLLM |
| `cloud` | a vendor API | OpenAI, Anthropic, Gemini, Groq, Mistral, xAI, OpenRouter, Cerebras, NVIDIA, Hugging Face |

`brain.prefer_local = true` (the default) means a healthy local model wins unless the request needs a
capability it does not advertise (vision, a larger context window, tool calling).

## Adding a provider

**Cloud, via the CLI** (key goes to the vault, never to a file):

```bash
natasha providers add openai --secret "$OPENAI_API_KEY"
natasha providers models            # discovery: what the provider actually serves
natasha providers test --provider openai
natasha providers list              # health, including the failure reason when unhealthy
```

**Any OpenAI-compatible endpoint** (works for a private gateway or a local server not in the
catalogue):

```toml
[providers.openai_compatible]
enabled = true
base_url = "http://192.168.1.10:9000/v1"
credential_ref = "credential://my-gateway"
default_model = "my-model"
local = true          # set false if it is not on your machine
privacy_tier = "private_cloud"
```

**A new adapter** for a protocol that is not OpenAI-shaped: subclass `ProviderAdapter` in
`backend/natasha/brain/adapters/`, declare `list_models`, `chat` and optionally `stream_chat`, then
register it. `tests/integration/conftest.py` ships a `ScriptedProvider` you can copy as a template.

## Discovery, health and honesty

* `POST /api/providers/{name}/discover` asks the provider what it serves and registers the models
  with their real capabilities and context windows. Discovery is explicit — nothing is assumed.
* `POST /api/providers/{name}/check` performs a real health call and records latency.
* `/api/providers` reports `enabled`, `has_credential`, `local`, `privacy_tier`, capability flags,
  the last health verdict and the discovered models. An unreachable provider shows as unhealthy with
  the error text, never as "fine".
* Enabling a provider that has never been registered works: the registry consults the configured
  catalogue and builds the adapter (`tests/unit/test_provider_toggle.py`).

## Routing

`Router` scores candidate models per task (`chat`, `code`, `vision`, `embedding`, `long_context`, …):

* capability match is a hard filter (a model that cannot see images is never chosen for an image),
* privacy tier is respected (`prefer_local`, and a request marked local-only never leaves the box),
* quality, cost, context window and observed latency are weighted (`brain.routing_weights`),
* `routing_weights` are per provider, so a private deployment can pin traffic.

`GET /api/providers` returns the routing configuration and recent usage, and `PATCH
/api/providers/routing` changes it (owner only, audited).

## Fallback, retries and limits

* Every call goes through `recovery` with bounded retries and a fallback ladder of
  `brain.fallback_depth` steps (default 3). A failed provider is marked unhealthy for
  `brain.health_ttl_seconds` and skipped while it stays that way.
* Retries reuse the same request id, so usage is not double-counted for a call that never happened.
* `429`/timeout/connection errors degrade to the next model; an auth error (`401`/`403`) does **not**
  silently fall back — it is surfaced, because it means the credential is wrong.
* Usage (tokens in/out, cost, latency, provider, model) is recorded per call and summarised at
  `GET /api/observability/usage`. Costs are used for routing pressure, not billing.

## Streaming

`stream_chat` is optional. When the chosen provider cannot stream, the answer arrives as a single
`token` frame after a `stream_unavailable` notice — the client is told, rather than being shown a fake
typewriter effect. The chat WebSocket and `POST /api/chat/stream` use the same code path.

## Errors you will actually see

| Symptom | Meaning | Action |
| --- | --- | --- |
| `provider 'x' is not registered` | never enabled or unknown name | `natasha providers enable x` |
| `401/403` from a provider | bad or expired key | `natasha credentials rotate x --secret …` |
| unreachable local server | Ollama/LM Studio not running | start it, then `providers models` |
| `no model can satisfy …` | capability/context mismatch | discover models, or lower the requirement |
| replies prefixed with an offline notice | no provider is configured | configure one; the placeholder is labelled, never passed off as an answer |

## Testing without a provider

`tests/integration` and `tests/e2e` inject a scripted provider, so the whole pipeline (tools,
approvals, missions, verification) is exercised without a network. That is why "the suite passes"
never depends on an API key — and why a live provider check is a separate, explicit step
(`natasha providers test`).
