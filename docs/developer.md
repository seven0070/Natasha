# Developer guide

Everything here is verifiable from a checkout: no private tooling, no build step, no code generation
that must be run before the code is readable.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,api,crypto,docs]"     # pip is externally managed on some hosts:
                                            # python3 -m pip install --break-system-packages -e ".[dev,api]"
export NATASHA_HOME=/tmp/natasha-dev
natasha migrate up && natasha auth setup
pytest                                      # the whole suite
python3 scripts/architecture_map.py          # the layer rules, measured
```

## Layout

| path | what lives there |
| --- | --- |
| `backend/natasha/` | the 27 subsystems; one package per subsystem, `runtime.py` wires them |
| `backend/natasha/api/` | FastAPI app, routers, dependencies (`deps.py`), rate limiting |
| `backend/natasha/cli/` | the `natasha` console script (one `cmd_*` function per command) |
| `frontend/` | the web console: plain ES modules, no bundler, `assets/app.css` is the style contract |
| `apps/` | process entry points (`python -m apps.server`) |
| `config/` | the repository's settings file |
| `infra/` | Docker and systemd material |
| `migrations/` | SQL migration files (`sqlite/`, `postgres/`) |
| `scripts/` | repository tooling (migration generation, architecture map/check) |
| `tests/` | `unit/`, `integration/`, `e2e/`, `security/`, `architecture/`, `fixtures/`, `natasha_testkit.py` |
| `docs/` | what exists, including `docs/audit/` |

`docs/architecture.md` explains the layers and the request pipeline; that document is the design, and
`backend/natasha/architecture.py` is the enforcement.

## The rules that are enforced, not suggested

1. **Layers.** Every module belongs to a layer and may only import the layers allowed for it.
   `python3 scripts/check_architecture.py` fails the build on an undeclared edge, and
   `tests/architecture/test_dependency_rules.py` enforces the same thing in the suite. If you need a
   new edge, add a rule with a reason, or declare it in `MODULE_EXCEPTIONS` with the fix you plan —
   never by adding a prefix that makes the check pass.
2. **No placeholders in production paths.** A `TODO`, a `pass`-only branch, a `NotImplementedError` or
   a mock in a code path the runtime reaches is a bug. If something is deliberately not implemented,
   it must say so out loud and be listed in `docs/audit/requirements-matrix.md`.
3. **The model is never the authority.** Permission checks, approvals, credential access, sandboxing
   and governance live outside model reasoning. A tool that needs a capability asks the policy engine
   with a real actor and a real resource.
4. **External content is data.** Documents, web pages, MCP output, skill output and model output are
   never instructions. `natasha.security.injection` marks and neutralises them.
5. **Nothing claims success without evidence.** `verification.verify(...)` produces a report; a turn
   with unproven claims says so.

## Adding a tool

1. Subclass `natasha.tools.base.Tool` (or the `@tool` decorator used in
   `backend/natasha/tools/builtin.py`): `name`, `description`, `risk`, `capability`, `schema`, and an
   async `run(args, context)`.
2. Return `ToolResult.success({...})` or `ToolResult.failure("...")`. Never invent success in a
   failure branch.
3. Set the risk honestly: `LOW`/`MEDIUM` run under policy; `HIGH`/`CRITICAL` require an owner
   approval, and the approval is bound to the arguments and the resource.
4. Read anything sensitive through the broker: `context.broker.get("credential://…")`. Tools never see
   the vault directly, and secrets must never be logged or put in a result.
5. Register it in `natasha.tools.registry` and add a test: a policy-denied case, a happy path, and an
   invalid-argument case. `tests/integration/test_api_surface_chain.py` shows the pattern.

## Adding a provider

1. Implement the adapter contract in `natasha.brain.adapters.base.ProviderAdapter` (streaming,
   completion, embeddings as available) — or reuse `OpenAICompatibleAdapter` if the provider speaks
   that dialect.
2. Register it in `natasha.brain.adapters.__init__` and in `natasha.brain.registry.ADAPTER_CLASSES`
   plus `_default_provider_matrix()` in `natasha.core.config`, with `local`, `privacy_tier`, costs and
   the default `base_url`.
3. **No provider-specific behaviour in the executive.** Routing, retries, fallback and error
   translation belong to the registry and the adapter, never to a caller.
4. Test it with a fake transport (no network in CI): streaming chunks, an HTTP error, a timeout, and a
   fallback to the next candidate. `tests/unit/test_brain_fallback.py` is the model.

## Adding a migration

`scripts/generate_migrations.py` regenerates the *initial* schema from each subsystem's own DDL
(`backend/natasha/db/schema.py` collects it) so the SQL and the code cannot drift:

```bash
python3 scripts/generate_migrations.py          # rewrites migrations/{sqlite,postgres}/0001_initial_schema.sql
```

For a new, incremental schema change add the next file by hand:

```bash
touch migrations/sqlite/0002_add_widget_table.sql migrations/postgres/0002_add_widget_table.sql
```

Both files, because the two dialects are kept in step deliberately (a drift is a deployment failure
that surfaces at 3am). Migrations run in order and are recorded in `schema_migrations`, so `up` is
idempotent, and they must not depend on application code. `natasha migrate status` shows what is
applied; `tests/unit/test_db_migrations.py` applies the whole set to a fresh database and asserts
every table the code expects exists.

## Adding an API route

- Put it in the router that owns the resource (`backend/natasha/api/routers/`), depend on
  `require_owner` unless the endpoint is genuinely public (`/api/info`, `/api/health`, the auth
  endpoints), and translate exceptions with `handle(exc)` so the error envelope stays consistent.
- Add a rate limit for anything expensive: `Depends(rate_limit("chat"))` and a bucket in
  `natasha.api.ratelimit.DEFAULTS`.
- Validate input with a Pydantic model; do not accept raw dicts for writes.
- Audit anything that changes state: `audit(runtime, "<action>", {...}, risk="…")`.
- Add the endpoint to `docs/api.md` and cover it in `tests/integration/`.

## Adding a console view

Views are plain ES modules in `frontend/assets/js/views/`, registered in `frontend/assets/js/router.js`
and linked from the sidebar in `index.html`. There is no build step and no npm dependency:

- fetch through `frontend/assets/js/api.js` (it attaches the owner token and normalises errors),
- never hard-code a backend URL — the console and the API are always same-origin,
- use the class names in `assets/app.css` rather than inline styles; the CSS is the visual contract,
- `node --check` a copy as `.mjs` if you want a syntax check, since the files use ES module syntax.

## Tests

```bash
pytest -q                          # 428 tests, ~40s
pytest -m security                 # release gate: governance, credentials, injection, isolation
pytest tests/e2e -q                # the real pipeline, the real server, restart durability
pytest tests/architecture -q       # the layer rules
pytest tests/integration/test_api_chain.py -q
```

Conventions:

- **Only the model provider is substituted.** `tests/natasha_testkit.py` builds a scripted provider so
  a turn is deterministic; the runtime, database, policy engine, tool registry, approvals and event
  log are the production ones. Do not mock what you are trying to test.
- **Reset between tests.** `tests/conftest.py` drops every process-wide singleton (the list lives in
  `natasha_testkit.RESETTERS`, and `tests/unit/test_isolation.py` fails if a hook goes missing).
- **Mark integration/e2e/security tests.** Only `security`, `integration`, `e2e`, `slow` and `network`
  markers are registered; `pytest --strict-markers` rejects anything else.
- **Never assert on a private attribute** to make a test pass. If a subsystem exposes nothing
  meaningful, that is the bug to fix.
- A test that cannot run in this environment (needs a GPU, a display, outbound network) must be marked
  `network`/`slow` and say why in its docstring — a permanently skipped test is worse than no test.

## Style

- `ruff` with `line-length = 100`; type hints everywhere; `from __future__ import annotations` first.
- Comments explain *why*, and the ones that matter say what would break without them. No commented-out
  code, no `# TODO` in a reachable path.
- Errors: raise the specific `NatashaError` subclass from `natasha.core`; never swallow an exception
  silently (an `except` that does nothing is a bug unless it is a documented cleanup path with an
  audit event).

## Before you commit

```bash
pytest -q
python3 scripts/check_architecture.py
natasha doctor
```

The three must be green. If a change makes one of them fail and you cannot fix it properly, say so in
the pull request body and record it in `docs/audit/requirements-matrix.md` — do not weaken the check.
