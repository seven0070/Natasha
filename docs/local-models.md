# Local models

Local-first is the default posture: `brain.prefer_local = true`, and a request marked
`privacy=local` never leaves the machine. This page covers the four shapes of local server Natasha
speaks to, and how to prove a real model is answering.

## 1. Ollama

```bash
ollama serve                      # http://127.0.0.1:11434
ollama pull llama3.1:8b

natasha providers add ollama
natasha providers enable ollama
natasha providers models          # discovery goes through /api/tags
natasha providers test --provider ollama
```

Config: `[providers.ollama] base_url = "http://127.0.0.1:11434"`, `local = true`,
`privacy_tier = "local"`. Vision models (`llava`, `qwen2-vl`) advertise image support through
discovery, and Natasha only routes an image to a model that reports it.

## 2. llama.cpp / GGUF

Run the server over a GGUF file:

```bash
llama-server -m ~/models/llama-3.1-8b-instruct.Q5_K_M.gguf --port 8080 -c 8192
```

```bash
natasha providers enable llamacpp      # base_url http://127.0.0.1:8080
```

`llamacpp` is a first-class provider in the catalogue; it is an OpenAI-compatible server, so tool
calls come from the server's `--jinja` chat template support. If the server does not support tools,
Natasha notices at discovery time and routes tool-using tasks away from it.

## 3. LM Studio

Start the local server in LM Studio (Developer → Start Server, default port 1234):

```bash
natasha providers enable lmstudio       # base_url http://127.0.0.1:1234/v1
natasha providers models
```

## 4. vLLM (and any OpenAI-compatible server)

```bash
python -m vllm.entrypoints.openai.api_server --model Qwen/Qwen2.5-14B-Instruct --port 8001
natasha providers enable vllm           # base_url http://127.0.0.1:8001/v1
```

Any other OpenAI-compatible endpoint (text-generation-webui, TGI, a company gateway) goes in the
`openai_compatible` slot — see [providers.md](providers.md#adding-a-provider).

## What discovery checks

`POST /api/providers/{name}/discover` (or `natasha providers models`) records, per model:

* the identifier the server actually offers (`/v1/models`, `/api/tags`),
* the context window it reports (or a conservative default when it reports nothing),
* whether it can call tools, stream, see images, and produce embeddings,
* the privacy tier and locality, so routing can respect a local-only request,
* latency and price hints where known.

Nothing is invented: a model that does not advertise a capability is never used for it.

## Local-only mode

```toml
[brain]
prefer_local = true
routing_weights = { ollama = 2.0 }     # a strong nudge toward the local provider

[providers.openai]
enabled = false                        # nothing to fall back to off-machine
```

With no cloud provider enabled, a request that a local model cannot serve fails honestly
(`ProviderUnavailable: all providers failed: …`) instead of silently sending your data to a vendor.
The `echo` provider is a labelled placeholder that never wins routing while any real model is healthy;
its answers are prefixed with an offline notice so they can never be mistaken for a real reply.

## Proving a local model actually works

The important half is not "the provider is listed" but "a real token came from a real model":

```bash
natasha providers test --provider ollama         # one-shot completion, prints provider/model/reply
natasha ask "say hello in five words"            # full executive path: context, tools, verification
curl -s localhost:8000/api/observability/usage -H "X-Natasha-Token: $TOKEN"   # tokens + latency
```

`usage` is the receipt: if it shows `tokens_in`/`tokens_out` for `provider=ollama`, the response came
from the local model rather than from a fallback.

## Notes and limits

* GPU/CPU performance is whatever the server gives; Natasha does not manage model loading.
* Embeddings use the configured model when one advertises the embedding capability, otherwise a
  local hashing embedder keeps retrieval working offline (lower quality, never a network call).
* Local servers are health-checked with a TTL (`brain.health_ttl_seconds`, default 90 s), so a
  crashed server is noticed within that window and skipped by routing.
* Speech and vision engines prefer local implementations when the optional extras are installed
  (`pip install -e ".[all]"`); otherwise they report `available: false` instead of pretending.
