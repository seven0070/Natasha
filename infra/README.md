# Infrastructure

Everything here is deployment-shaped: it starts, isolates or supervises the agent, and it contains no
application logic.

| path | what it is |
| --- | --- |
| `docker/entrypoint.sh` | container entry point: prepares `$NATASHA_HOME`, applies migrations, then serves |
| `systemd/natasha.service` | hardened unit for a headless server install |
| `../Dockerfile` | the image (non-root, tini, healthcheck, `/data` volume) |
| `../docker-compose.yml` | the agent, plus optional `local-models` (Ollama) and `postgres` profiles |
| `../.env.example` | the environment a deployment actually sets (no secrets - by design) |

## Container

```bash
docker build -t natasha .
docker run --rm -p 127.0.0.1:8000:8000 -v natasha-data:/data natasha
docker compose exec -it natasha natasha auth setup      # set the owner passphrase (first run)
```

The entry point runs `natasha migrate up` before serving, because the database lives in the volume
while the schema lives in the image. `NATASHA_SKIP_MIGRATIONS=1` skips that step for operators who
migrate out of band.

Migrations are idempotent and recorded (`natasha migrate status`), so restarting the container is safe.

## systemd

```bash
sudo cp infra/systemd/natasha.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now natasha
```

The unit binds to `127.0.0.1` on purpose: this profile is headless, and a reverse proxy should
terminate TLS in front of it. `ProtectSystem=strict` with `ReadWritePaths=/var/lib/natasha` means a
compromised agent process still cannot write anywhere else on the host. Computer-use (screen capture,
input injection) is not available to this unit: it has no display and no permission to reach one.

## What is deliberately *not* in this directory

- **No secrets, ever.** Not in the image, not in the compose file, not in a unit file. Provider keys
  are stored in the encrypted vault inside `$NATASHA_HOME` and read through the credential broker at
  call time; `natasha.core.config` ignores `NATASHA_API_KEY*`/`NATASHA_SECRET*`/`NATASHA_TOKEN*` on
  purpose.
- **No desktop packaging.** Tauri, the system tray and platform-native integration are deferred to
  repository phase 2 (see `docs/deployment.md`).
- **No reverse proxy.** Somebody's nginx/Caddy/Traefik belongs to that operator's infrastructure, not
  to this repository; `docs/deployment.md` documents what the proxy must preserve (the
  `X-Natasha-Token` header, WebSocket upgrade on `/api/chat/ws`, and the console's static files).
