# Deployment

Natasha runs as a single Python process that serves the REST/WebSocket API and the web console, and
stores everything it owns under one directory (`$NATASHA_HOME`). That is the whole deployment model:
no external queue, no cache, no reverse dependency. Model providers are *optional* services the agent
talks to over HTTP; if none is reachable it says so instead of inventing an answer.

- [Install paths](#install-paths)
- [Configuration layers](#configuration-layers)
- [Docker](#docker)
- [systemd](#systemd)
- [First run: the owner](#first-run-the-owner)
- [Migrations and the database](#migrations-and-the-database)
- [Health checks](#health-checks)
- [Backup and restore](#backup-and-restore)
- [TLS and remote access](#tls-and-remote-access)
- [Local model services](#local-model-services)
- [Clean-install verification](#clean-install-verification)
- [What is deferred](#what-is-deferred)

## Install paths

| path | use it when |
| --- | --- |
| source checkout + venv | development, or a server you want to `git pull` |
| Docker / compose | a repeatable deployment, or a machine you do not want to install Python on |
| systemd unit | a long-lived headless server install |

All three end in the same command (`natasha serve` / `python -m apps.server`); the container and the
unit only differ in how they set the environment and where the data lives.

## Configuration layers

Lowest to highest precedence:

1. built-in defaults (`backend/natasha/core/config.py`)
2. `config/natasha.toml` — the *checkout's* file, or `$NATASHA_CONFIG` when set (the image sets it to
   `/app/config/natasha.toml`)
3. `$NATASHA_HOME/config/natasha.toml` — the machine's own settings, written by the agent
4. `NATASHA_*` environment variables — nested keys use two underscores
   (`NATASHA_EXECUTIVE__MAX_STEPS=80`, `NATASHA_PROVIDERS__OLLAMA__BASE_URL=http://ollama:11434`)
5. explicit overrides in code

Invalid TOML raises `ConfigurationError` at load: a broken configuration file never degrades into
silently-ignored settings. A missing file is fine — every layer is optional.

**Credentials are not configuration.** `natasha.core.config` ignores any environment variable whose
first path segment is `api_key`, `secret` or `token`. Keys live in the encrypted vault under
`$NATASHA_HOME/vault` and are handed to a tool through the credential broker for one call at a time.

## Docker

```bash
docker build -t natasha .
docker compose up -d                       # → http://127.0.0.1:8000
docker compose exec -it natasha natasha auth setup
```

What the image does:

| concern | how |
| --- | --- |
| data | one named volume at `/data` (`NATASHA_HOME`); the vault key never lands in a source tree |
| user | non-root (`natasha`, uid 10001); only `/data` is writable |
| signals | `tini` as PID 1 so Ctrl-C and `docker stop` reach Python |
| migrations | `infra/docker/entrypoint.sh` runs `natasha migrate up` before serving (idempotent; `NATASHA_SKIP_MIGRATIONS=1` skips it) |
| health | `HEALTHCHECK` on `/api/health`, checking `runtime.ok`, not just HTTP 200 |
| bind | `NATASHA_HOST=0.0.0.0` inside the container; publish it on `127.0.0.1` unless a proxy fronts it |

Optional profiles: `--profile local-models` adds Ollama, `--profile postgres` adds PostgreSQL (see
[Migrations and the database](#migrations-and-the-database) for exactly what Postgres supports today).

## systemd

`infra/systemd/natasha.service` is the headless profile:

```bash
sudo useradd --system --home /var/lib/natasha --create-home natasha
sudo python3 -m venv /opt/natasha/.venv
sudo /opt/natasha/.venv/bin/pip install "/opt/natasha[api,crypto,docs]"
sudo -u natasha /opt/natasha/.venv/bin/natasha migrate up
sudo -u natasha /opt/natasha/.venv/bin/natasha auth setup
sudo cp /opt/natasha/infra/systemd/natasha.service /etc/systemd/system/
sudo systemctl enable --now natasha
```

The unit runs with `ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges` and
`ReadWritePaths=/var/lib/natasha`: the process cannot write outside its own home even if the model is
tricked into trying. It binds `127.0.0.1` and has no display, so computer-use is unavailable by
construction — that is the point of a server profile, not a missing feature.

## First run: the owner

Nothing works until the owner exists; until then every owner route returns 401.

```bash
natasha auth setup              # prompts for a passphrase (input not echoed)
natasha auth status             # is the owner set, how many sessions are live
natasha doctor                  # environment, providers, security posture, degraded subsystems
```

Then, in the console or CLI: enable a provider (`natasha providers add ollama` /
`natasha providers add openai`) and say hello.

## Migrations and the database

```bash
natasha migrate status          # applied versions and the pending set
natasha migrate up              # apply to head (what the container entry point runs)
natasha migrate down --steps 1  # revert the last migration
```

Migrations are plain SQL files under `migrations/sqlite/NNNN_name.sql` (`migrations/postgres/` mirrors
them), applied in order and recorded in a `schema_migrations` table, so running `up` twice is a no-op.

> **Honest status.** The SQLite path is what the runtime uses today, end to end. The PostgreSQL
> schema and migration runner exist and are exercised (`MigrationRunner(dialect="postgres", dsn=…)`
> with the `postgres` extra), but the runtime *stores* are SQLite-only and
> `memory.db_backend = "postgres"` is not honoured by them yet. Do not deploy Postgres expecting the
> agent to use it: `docs/audit/requirements-matrix.md` records this as PARTIAL.

## Health checks

| endpoint | auth | answers |
| --- | --- | --- |
| `GET /api/health` | public | `runtime.ok`, degraded subsystems, provider states, memory/mission counters |
| `GET /api/status` | owner | version, home, features, subsystem state |
| `GET /api/doctor` | owner | `overall`, and every check the agent performed with its detail |

A supervisor should use `/api/health` and require `runtime.ok`. A load balancer should not: the agent
is single-instance by design (one SQLite database, one event log, one vault), and two containers must
never share a volume.

## Backup and restore

```bash
natasha backup --path /backups/natasha-$(date +%F).tar.gz
```

The archive contains `config`, `db`, `skills`, `marketplace`, `artifacts`, `missions` and `mcp` — the
state that cannot be recreated. To restore, stop the agent, unpack into `$NATASHA_HOME`, and start it
again; migrations run on start.

Back up `vault/` and `keys/` together or not at all: the vault is encrypted and its key file is what
makes it readable. A backup without the key is a box of noise.

## TLS and remote access

The API refuses unauthenticated requests, requires a passphrase-derived owner token, and rate-limits
login attempts — but it speaks plain HTTP. If a client is not on the same machine, terminate TLS in
front of it (Caddy, nginx, Traefik) and keep the agent bound to loopback. The proxy must:

- forward the `X-Natasha-Token` header (and `Authorization: Bearer` if you use it),
- allow the WebSocket upgrade on `/api/chat/ws` with a long idle timeout,
- serve or pass through the console's static files unchanged (they are plain ES modules, no build step),
- not buffer server-sent streaming responses — the console streams tokens.

Do not expose the port directly on a public interface "for now". The host header, CORS and CSRF
protections cover browser abuse, not network abuse.

## Local model services

Local models are a separate service, and the agent treats them as just another provider:

| server | default endpoint |
| --- | --- |
| Ollama | `http://127.0.0.1:11434` |
| llama.cpp / GGUF | `http://127.0.0.1:8080` |
| LM Studio | `http://127.0.0.1:1234/v1` |
| vLLM | `http://127.0.0.1:8001/v1` |

In compose, point the provider at the service name
(`NATASHA_PROVIDERS__OLLAMA__BASE_URL=http://ollama:11434`). Discovery, health, routing and fallback
are described in [local-models.md](local-models.md).

## Clean-install verification

The acceptance path, run end to end on this machine (evidence is in `docs/audit/evidence.md`):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,api]"                 # 1. install
export NATASHA_HOME=/tmp/natasha-clean
natasha migrate up                          # 2. create the database
natasha auth setup                          # 3. authenticate (owner passphrase)
natasha providers add ollama                # 4. a model (or any cloud provider; see providers.md)
natasha ask "hello"                         # 5. converse
natasha memory list                         # 6. memory
natasha missions create "write a note"      # 7. a mission with a real artifact
natasha skills install ./my-skill && natasha skills run my-skill --payload '{}'   # 8. a skill
natasha doctor                              # 9. what is healthy, what is not
natasha backup --path /tmp/natasha.tar.gz   # 10. back up
natasha serve                               # 11. serve, then Ctrl-C
natasha serve                               # 12. restart: state is still there
```

`tests/e2e/test_live_server.py` automates steps 2–12 against a real server process (including the
restart), and `tests/e2e/test_restart_persistence.py` asserts the durable state directly. If no model
provider is reachable, step 5 does not fail: the agent answers with an explicit offline placeholder
saying it has no model, which is the honest behaviour rather than a fabricated reply.

## What is deferred

**Desktop packaging (Tauri, tray, autostart, native permissions) is deferred to repository phase 2.**
The backend, API, console, tests, migrations and deployment story are finished first, and nothing here
depends on the desktop app. `tauri` is already on the forbidden-import lists so the boundary is ready
when that work starts. The requirement is tracked as DEFERRED in the audit matrix, not dropped.
