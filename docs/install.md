# Installing and starting Natasha

Natasha runs from a checkout: a Python backend (`backend/natasha`), a zero-build web console
(`frontend/`), SQL migrations (`migrations/`) and a CLI. There is no npm build step and no external
database requirement — SQLite is the default and PostgreSQL is an option.

## Requirements

| Requirement | Notes |
| --- | --- |
| Python 3.11+ | the code uses `tomllib`, `dataclass` features and modern typing |
| ~200 MB disk | the home directory holds the databases, artifacts and the encrypted vault |
| Optional: Docker | for the container deployment in [deployment.md](deployment.md) |
| Optional: Node 18+ | only for frontend syntax checks; the console is plain ES modules |

No compiler, no system libraries and no cloud account are required to start.

## Install

```bash
git clone <your-fork> natasha && cd natasha
python3 -m venv .venv && . .venv/bin/activate

pip install -e ".[api,dev]"          # backend + API + test tooling
# on a system without venv support (e.g. containers with an externally-managed interpreter):
python3 -m pip install --break-system-packages -e ".[api,dev]"
```

Extras: `api` (FastAPI/uvicorn), `postgres`, `crypto` (hardened vault backends), `docs`, `desktop`,
`dev`, `all`. The base install is `pydantic` + `httpx`; everything else degrades gracefully — the
runtime reports a subsystem as unavailable instead of crashing.

Set the home directory (all state lives here):

```bash
export NATASHA_HOME="$HOME/.natasha"
```

## First run

```bash
natasha status          # what is wired up, and what is not
natasha doctor          # environment, security and provider self-check
```

`natasha status` and `natasha doctor` work before any owner exists. `doctor` prints findings with a
severity, so an unconfigured provider reads as a warning rather than a failure.

### Create the owner

```bash
natasha auth setup --passphrase 'correct horse battery staple' --owner owner
natasha auth status
```

The passphrase is stretched with PBKDF2-HMAC-SHA256 (600 k iterations) and only the salt + hash are
stored; sessions are SQLite-backed and can be revoked (`natasha auth revoke`). Through the API the
equivalent is `POST /api/auth/setup` followed by `POST /api/auth/login` — see
[api.md](api.md#authentication).

### Configure a model provider

Local (nothing leaves the machine):

```bash
# with Ollama running on http://127.0.0.1:11434
natasha providers add ollama
natasha providers models
```

Cloud (the key is written to the encrypted vault — never to a settings file or a log):

```bash
natasha providers add openai --secret "$OPENAI_API_KEY"
natasha providers enable anthropic --secret "$ANTHROPIC_API_KEY"
natasha providers test --provider openai
```

Keys can also be supplied as `NATASHA_PROVIDERS__OPENAI__...`? **No** — environment variables whose
first path segment is `api_key`, `secret` or `token` are deliberately ignored, so a key cannot be
injected through the environment. Use the vault (above) or the console's Providers screen.

### Talk to it

```bash
natasha ask "summarise my open missions"     # one shot
natasha chat                                  # interactive, streaming
natasha serve --host 0.0.0.0 --port 8000      # API + console
```

Open `http://127.0.0.1:8000/`, log in with the owner passphrase, and the console's Chat screen is the
default route.

## Verifying the installation

```bash
python3 -m pytest tests -q                     # full suite (unit, integration, security, e2e)
python3 -m pytest tests/security -q            # release gate
natasha doctor --json                          # machine-readable self-check
natasha activity verify                        # audit hash chain
natasha migrate status                         # database schema state
```

The end-to-end suite starts a real `natasha serve` process over HTTP, creates the owner, runs a
mission through an approval, restarts the process and checks that memory survived. If that passes on
your machine, the installation is functional.

## Where things live

```
$NATASHA_HOME/
  config/natasha.toml     effective settings (written by `natasha ... save` / the Settings screen)
  db/events.db            append-only, hash-chained audit log
  db/memory               durable memory (SQLite)
  db/missions.db          missions and steps
  db/auth.db              owner credentials + sessions
  vault/                  encrypted credentials (master key in keys/)
  artifacts/              created files, documents, images, audio
  uploads/                files you attached
  workspace/              the directory Natasha may write in without an approval
  logs/                   rotating logs (no secrets: the sanitizer runs first)
  reports/                exports, backups, verification reports
```

Deleting a single file under `db/` loses that subsystem's state; deleting `keys/` makes the vault
unreadable. Back up with `natasha backup` ([deployment.md](deployment.md#backup-and-restore)).
