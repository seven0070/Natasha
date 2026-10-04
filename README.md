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
pip install -e ".[api,dev]"        # add ,crypto,docs for the encrypted vault and document readers

export NATASHA_HOME="$HOME/.natasha"   # optional: the default, shown for clarity

natasha migrate up                 # create the database (runs on every container start too)
natasha auth setup                 # set the owner passphrase — nothing is reachable before this
natasha doctor                     # environment + provider + security self-check
natasha providers add ollama       # or openai / anthropic / gemini / ... (prompts for a key)
natasha ask "plan my week"         # one-shot turn; `natasha chat` is the interactive one
natasha serve                      # API + web console on http://127.0.0.1:8000
```

Docker instead of a checkout:

```bash
cp .env.example .env && docker compose up -d
docker compose exec -it natasha natasha auth setup
```

Full paths: [docs/install.md](docs/install.md) · [docs/deployment.md](docs/deployment.md).

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
| `backend/natasha/api`, `cli` | REST + WebSocket API, the `natasha` console script |
| `frontend/` | dependency-free web console (Chat, Voice, Missions, Memory, …) — no build step |
| `apps/` | process entry points (`python -m apps.server`) |
| `config/` | the repository settings file and what belongs in which layer |
| `infra/` | Docker and systemd material |
| `migrations/` | SQL migrations, SQLite and PostgreSQL |
| `packages/` | shared packages (currently the committed API contract) |
| `scripts/` | repository tooling: migrations, the architecture map and its CI gate |
| `desktop/` | Tauri shell — **deferred to repository phase 2** (see `docs/deployment.md`) |

## Status

Honest, requirement-by-requirement status — including what is **not** implemented and what is
deferred — lives in [`docs/audit/requirements-matrix.md`](docs/audit/requirements-matrix.md), with the
commands and results behind each claim in [`docs/audit/evidence.md`](docs/audit/evidence.md).
Read them before deploying.

The layering rules (which module may import which) are declared in
`backend/natasha/architecture.py` and enforced by `tests/architecture/` — the measurement, the
remaining reasoned exceptions and the plan to remove them are in
[ARCHITECTURE_REFACTOR_REPORT.md](ARCHITECTURE_REFACTOR_REPORT.md).

## Tests

```bash
pytest                      # unit + integration + security + e2e + architecture
pytest -m security          # release gate: governance, credentials, injection, isolation
python3 scripts/check_architecture.py       # the layer gate
python3 scripts/export_openapi.py --check   # the API contract gate
```

## License

See `LICENSE`.
