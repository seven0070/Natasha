# Natasha Agent

A persistent, multimodal, local-first personal AI agent: conversation, reasoning, planning,
research, coding, memory, local + cloud LLMs with automatic routing, MCP, skills, plugins,
marketplace, browser/computer control, voice, vision, missions, verification, recovery and
governed self-improvement.

Natasha is a **cognitive operating system**: the model is a component, never the authority.
Governance, permissions, credentials, audit and protected upgrades live *outside* model reasoning
and are enforced by code.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,api]"

natasha init                       # create NATASHA_HOME, generate master key, migrate DB
natasha doctor                     # environment + provider + security self-check
natasha providers add ollama       # or openai / anthropic / gemini / ...
natasha chat "plan my week"        # terminal chat (streaming)
natasha serve                      # API + web UI on http://127.0.0.1:8000
```

Then open the UI and complete owner setup. See `docs/INSTALLATION.md`.

## Layout

| Path | Purpose |
| --- | --- |
| `backend/natasha/core` | ids, clock, config, hashing, errors, paths |
| `backend/natasha/events` | append-only hash-chained event log, sanitizer |
| `backend/natasha/security` | policy engine, capability scoping, arg validation, injection defense |
| `backend/natasha/governance` | constitution, identity, upgrade governor, rollback |
| `backend/natasha/approvals` | signed, scoped, expiring approval engine |
| `backend/natasha/credentials` | encrypted vault, broker, OAuth2/PKCE/device flows |
| `backend/natasha/memory`, `world` | ten memory kinds, hybrid retrieval, world model |
| `backend/natasha/brain` | provider + model registry, routing, fallback, usage |
| `backend/natasha/perception`, `voice`, `computer` | vision, hearing, speech, computer use |
| `backend/natasha/tools`, `mcp`, `integrations` | tool runtime, MCP client, connectors |
| `backend/natasha/skills`, `marketplace` | skill runtime, marketplace security pipeline |
| `backend/natasha/agents`, `missions` | supervisor/workers, durable missions |
| `backend/natasha/creation`, `affect` | multimodal creation engine, affective state |
| `backend/natasha/executive` | the Natasha executive loop |
| `backend/natasha/verification`, `recovery` | verification engine, failure recovery |
| `backend/natasha/api`, `cli`, `sdk` | REST + WebSocket, CLI, Python/plugin SDKs |
| `frontend/` | dependency-free SPA (Chat, Voice, Missions, Memory, …) |
| `desktop/` | Tauri 2 shell |

## Status

Honest per-subsystem status — including what is **not** implemented — lives in
[`docs/STATUS.md`](docs/STATUS.md). Read it before deploying.

## Tests

```bash
pytest                      # unit + integration + security + e2e
pytest -m security          # release gate
```

## License

See `LICENSE`.
