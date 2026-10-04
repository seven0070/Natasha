# API

The API is a FastAPI app (`natasha.api.app.create_app`) serving 152 REST paths and one WebSocket
under `/api`, plus the web console at `/` when `serve_ui=True`. The OpenAPI document is generated at
runtime: `GET /openapi.json` (or `python3 -c "from natasha.api.app import create_app;
print(len(create_app(serve_ui=False).openapi()['paths']))"`).

## Authentication

```bash
curl -s localhost:8000/api/auth/status
# {"initialised": false}

curl -s -X POST localhost:8000/api/auth/setup \
     -H 'content-type: application/json' \
     -d '{"passphrase": "correct horse battery staple", "owner_id": "owner"}'
# {"ok": true, "token": "...", "owner_id": "owner"}

TOKEN=$(curl -s -X POST localhost:8000/api/auth/login \
        -H 'content-type: application/json' \
        -d '{"passphrase": "correct horse battery staple", "client": "cli"}' | jq -r .token)

curl -s localhost:8000/api/memory -H "X-Natasha-Token: $TOKEN"
```

* `POST /api/auth/setup` is **loopback-only**: it refuses a non-local client, so a fresh install
  cannot be claimed from the network.
* Every other owner endpoint requires the `X-Natasha-Token` header and returns `401` without it.
* Sessions are stored in `db/auth.db`, expire after `session_ttl_minutes` (default 12 h), and can be
  listed/revoked (`GET /api/auth/sessions`, `DELETE /api/auth/sessions/{id}`).
* The passphrase can be rotated with `POST /api/auth/passphrase` (old + new required).

## Errors

`natasha.api.deps.handle` maps the domain exceptions onto HTTP once, so every endpoint behaves the
same way:

| Exception | Status | Meaning |
| --- | --- | --- |
| `PolicyDenied` | 403 | the policy refuses this capability for this actor |
| `ApprovalRequired` | 409 | the action needs an owner decision first |
| `ConflictError` | 409 | the request contradicts current state |
| `NotFoundError` | 404 | no such provider/mission/memory/… |
| `ValidationError` | 422 | the payload is malformed |
| `NatashaError` | 400 | other domain errors |
| anything else | 500 | unexpected; the client gets a generic message, the log gets the detail |

Error bodies are `{"detail": "..."}`. Secrets are redacted by the sanitizer before anything is
logged, so a 500 body never contains a credential.

## Rate limits

Every expensive or security-sensitive endpoint is behind an in-process token bucket
(`natasha.api.ratelimit`). The bucket is keyed by client address **and** session token, so one client
cannot spend another's budget. Exceeding it returns `429` with a `Retry-After` header:

```json
{"detail": "too many requests for 'chat'; retry in 0.9s"}
```

| Bucket | Default | Guards |
| --- | --- | --- |
| `chat` | 60/min, burst 12 | `POST /api/chat`, `/api/chat/stream`, `WS /api/ws/chat` |
| `models` | 30/min | provider checks, discovery, test calls |
| `missions` | 20/min | mission create/run/resume/verify |
| `creation` | 10/min | `/api/creation/create` |
| `computer` | 60/min | click/type/key/launch |
| `voice`, `vision`, `mcp`, `skills` | 30/min each | the corresponding action endpoints |
| `login` | 10/min, burst 5 | `POST /api/auth/setup`, `POST /api/auth/login` (passphrase guessing) |
| `write` / `default` | 120/min / 240/min | the remaining mutating and read endpoints |

Tune or disable it under `[limits]` in the settings file
(`enabled`, `multiplier`, `overrides = { chat = [120, 24] }`). Tests for this behaviour live in
`tests/integration/test_rate_limits.py`.

## Validation

* Request bodies are Pydantic models (`natasha.api.models`) with explicit bounds — a chat message,
  mission objective or memory content has a maximum length, and an oversized payload is a `422`.
* Expensive endpoints (`/api/chat`, `/api/missions`, `/api/creation/create`) are owner-gated and
  audited; the event log records actor, trace id, risk and outcome.
* The WebSocket requires the same token, and an unauthenticated connection receives an error frame
  and is closed rather than being silently accepted.

## Surface map

| Group | Highlights |
| --- | --- |
| `/api/auth` | status, setup, login, logout, passphrase, sessions (+revoke) |
| `/api/chat` | `POST /api/chat`, `POST /api/chat/stream` (SSE), `WS /api/ws/chat` |
| `/api/conversations` | list, get, delete, history |
| `/api/missions` | create/plan/run/pause/resume/cancel/verify/rollback, stats, resumable |
| `/api/memory` | add, list, recall, correct, forget, stats, working, world, kinds |
| `/api/approvals` | pending, history, get, approve, deny, revoke, expire |
| `/api/activity` | tail, query, stats, `verify` (hash chain), export (JSONL) |
| `/api/security` | audit, broker (credential access grants), credentials (+rotate/test/health/revoke), sessions |
| `/api/governance` | constitution, invariants, manifest, upgrades (+analyse/apply/request-approval/rollback/test/opportunities) |
| `/api/skills`, `/api/marketplace`, `/api/mcp` | full lifecycles (see [skills.md](skills.md), [marketplace.md](marketplace.md), [mcp.md](mcp.md)) |
| `/api/integrations` | list, connect, disconnect, perform |
| `/api/agents` | roles, workers, delegate, profiles |
| `/api/providers` | list, check, discover, enable, disable, routing |
| `/api/computer` | capabilities, screenshot, click, type, key, launch, clipboard, history, browser/* |
| `/api/voice` | loop, turn (+audio, +file), interrupt, capabilities, voices, history, listen, speak, transcribe |
| `/api/vision` | capabilities, analyse, ocr, screenshot, camera, video, documents, accessibility |
| `/api/creation` | capabilities, create, jobs |
| `/api/artifacts` | list, content, download |
| `/api/tools` | list, describe, execute |
| `/api/observability` | health, metrics, traces, usage |
| `/api/settings` | get, patch (mutable subset), save |
| `/api` | info, health, status, doctor |

## Streaming

```bash
curl -N -X POST localhost:8000/api/chat/stream \
  -H "X-Natasha-Token: $TOKEN" -H 'content-type: application/json' \
  -d '{"message": "what did I ask you yesterday about the migration?"}'
```

Server-sent events: `turn_started`, `token`, `tool`, `round`, `turn_finished` (and
`stream_unavailable` if no provider can stream — the answer then arrives in one piece and the client
is told, instead of being shown fake incremental output). The WebSocket carries the same frame
types.

## Examples

Run a mission with a step that needs approval, then approve it:

```bash
MISSION=$(curl -s -X POST localhost:8000/api/missions -H "X-Natasha-Token: $TOKEN" \
  -H 'content-type: application/json' -d '{
    "objective": "Write a short status report",
    "success_criteria": ["artifact_exists"],
    "verification_plan": ["artifact_exists"],
    "steps": [{"title": "write", "kind": "tool", "tool": "write_artifact",
               "arguments": {"name": "status.md", "content": "# Status\n"}}]
  }' | jq -r .mission.id)

curl -s -X POST "localhost:8000/api/missions/$MISSION/verify" -H "X-Natasha-Token: $TOKEN"
```

Verify the audit chain and export it:

```bash
curl -s localhost:8000/api/activity/verify -H "X-Natasha-Token: $TOKEN"
curl -s -o events.jsonl localhost:8000/api/activity/export -H "X-Natasha-Token: $TOKEN"
```

## Versioning

The console and CLI are shipped with the backend and are versioned together. Additive fields are
safe; breaking changes get a new path (`/api/v2/...`) and the old one keeps working until the
console no longer calls it. `GET /api/info` reports the version the running process serves.
