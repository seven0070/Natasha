# Troubleshooting

Start here. `natasha doctor` answers most questions without guessing, and the event log answers the
rest.

```bash
natasha doctor                 # every check the agent ran, and what it found
natasha doctor --json          # the same, machine-readable
natasha status                 # version, home, host/port, features, degraded subsystems
natasha activity tail --limit 50   # the tail of the hash-chained audit log
natasha activity verify            # is the audit chain intact?
```

`natasha doctor` prints one line per check (`OK`/`WARN`/`FAIL` + area + detail), then the
`capabilities` block (voice, computer, browser, creation) with an explicit reason when a capability is
missing, then the tool and provider counts. The API's `GET /api/doctor` returns the same checks as
`{"home", "overall", "findings"}`. `overall` is `degraded` whenever a subsystem reported a problem:
the agent keeps running and keeps saying which capability is unavailable instead of pretending it
exists.

---

## "It answers, but not with a model"

Symptom: replies contain a placeholder saying no model provider is available.

Cause: no provider is enabled, or the enabled one is unreachable. This is deliberate — the agent never
fabricates an answer.

```bash
natasha providers list          # state + detail per provider
natasha providers health        # re-check reachability now
natasha providers models        # what the enabled providers actually serve
natasha providers test --provider ollama   # one real completion against one provider
```

- **Local server not running.** `connection refused`/`ConnectError` for
  `http://127.0.0.1:11434` means Ollama (or llama.cpp/LM Studio/vLLM) is not listening. Start it and
  re-check; see [local-models.md](local-models.md).
- **Cloud provider enabled but rejected.** A `401`/`403` means the key in the vault was refused:
  `natasha credentials test openai`, then rotate with `natasha credentials rotate openai`.
- **TLS/network errors to a cloud provider** are reported as `ConnectError` details. In a sandbox with
  no outbound network this is expected and is not a bug in the agent.
- **Routed to the wrong provider.** Routing prefers local models when `brain.prefer_local` is true and
  a local model is healthy. `natasha providers models` shows what is registered;
  `natasha doctor` shows the router's decision inputs.

## "Every request returns 401"

The owner has not been set up, or this client has no session token.

```bash
natasha auth status             # initialised? how many sessions?
natasha auth setup              # create the owner (prompts for a passphrase)
natasha auth passphrase         # change it (requires the current one)
```

The console stores its token after login. A raw HTTP client needs the header:

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"passphrase":"…","client":"cli"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -s -H "X-Natasha-Token: $TOKEN" localhost:8000/api/status
```

If logins start failing with 429, you have tripped the login rate limit (10 attempts, burst 5). Wait
for the `Retry-After` seconds; the limit is per client key and lives in memory, so a restart clears it.

## "429 Too Many Requests"

Working as designed: expensive routes are token-bucketed (`chat 60/12`, `models 30/6`,
`creation 10/3`, …). Every 429 carries `Retry-After`. To raise a limit for a real workload, change the
multiplier instead of disabling the guard:

```toml
[limits]
enabled = true
multiplier = 2.0
```

`limits.enabled = false` exists for load tests; do not ship it. See [configuration.md](configuration.md).

## "A tool did nothing, and I was not asked"

An action that is not allowed by policy is **refused**, not queued: the turn reports the refusal and
the tool call's error. If the action is HIGH/CRITICAL risk, the agent must ask first, and the request
lands in the approvals queue:

```bash
natasha approvals list          # pending requests, with their operation and risk
natasha approvals history       # what was decided, by whom, when
natasha approvals approve <id> --note "why"     # or: natasha approvals deny <id>
natasha approvals revoke <id>   # withdraw a grant that was not used yet
```

Approvals are scoped to one operation + arguments + resource, expire, and are consumed on first use —
approving one file write does not approve a second one. If you approve and the action still does not
run, check the *fingerprint*: a changed argument invalidates the grant by design.

## "A mission is stuck"

```bash
natasha missions list
natasha missions show <id>          # steps, their states, the last observation
natasha missions verify <id>        # what the verification engine can and cannot prove
```

- `blocked` almost always means a step is waiting for an approval: decide it (`natasha approvals
  approve <id>`) and resume with `natasha missions run <id>`.
- `failed` step with a *safe* retry: `natasha missions run <id>` re-runs from the failed step, because
  the engine never blindly replays an irreversible action — it reports that instead.
- A step whose evidence is missing stays `unproven`; the mission cannot report success. That is the
  verification requirement, not a bug. `natasha missions verify <id>` lists exactly which checks
  failed.

## "The console is blank / assets 404"

The console is plain ES modules served by the API itself: no build step, no bundler, no CDN.

```bash
curl -s localhost:8000/ | head -5              # index.html
curl -sI localhost:8000/assets/app.css         # static assets
```

- If `/` returns 404, the app was created with `serve_ui=False`. Start it with `natasha serve`.
- If assets 404 behind a reverse proxy, the proxy is not passing sub-paths through. It must forward
  `/`, `/assets/*`, `/manifest.webmanifest` and `/api/*` unchanged (see
  [deployment.md](deployment.md#tls-and-remote-access)).
- If the page loads but nothing works, open the browser console: the UI surfaces API errors in
  `chat`/activity views, and a 401 means the session expired (re-login).

## "Port already in use"

```
OSError: [Errno 98] Address already in use
```

Something is already listening. Find it (`ss -ltnp | grep 8000`) or serve elsewhere:

```bash
natasha serve --port 8010
```

## "Database is locked" / a command hangs at startup

SQLite allows one writer; a second process pointed at the same `$NATASHA_HOME` will contend.

- The agent is single-instance by design. Do not run `natasha serve` twice against the same home, and
  do not point two containers at one volume.
- A crashed process can leave the write lock behind: make sure the old process is gone
  (`ps aux | grep natasha`), then retry. The database itself survives; `natasha migrate status` proves
  it is readable.

## "Permission denied" under `$NATASHA_HOME`

The vault, the database and the key file are `0700`/`0600` on purpose. Fix the ownership of the home,
not the permissions of the files:

```bash
sudo chown -R natasha:natasha /var/lib/natasha      # the deployment user, not root
```

Running the agent as root is not supported: the sandboxing and the file policy assume a normal user.

## "The audit chain does not verify"

```bash
natasha activity verify
natasha activity stats
natasha activity export --path /tmp/audit.jsonl
```

If it fails, stop and treat it as an incident: the chain is hash-linked precisely so a modified or
deleted event is detectable. Keep the database (`cp $NATASHA_HOME/db/natasha.db /tmp/`), note the
first broken event id, and do not keep writing to the same home. A fresh home plus `natasha backup`
of the intact parts is the recovery path; `docs/security.md` explains the threat model.

## MCP server problems

```bash
natasha mcp list                # states: discovered -> inspected -> approved -> enabled
natasha mcp install <name>      # connect, list the tools, scan them, show what must be approved
natasha mcp tools <name>        # the tool list as the agent sees it (namespaced)
natasha mcp enable <name>       # available to turns - only after the permissions were reviewed
natasha mcp health <name>       # is it up? which tools answer?
natasha mcp call <name> <tool> --arguments '{"…": "…"}'
natasha mcp uninstall <name>
```

- A server that is `discovered` but not `approved` cannot be called: MCP calls are policy-checked like
  any other tool, and its tools are quarantined until the owner approves the permissions.
- Tools that come back from an MCP server are *data*, never instructions. If a server's output tries to
  give orders, that text is marked untrusted and the governance layer ignores it; you will see it in
  the activity view as untrusted content, not as an action.
- `unreachable` means the transport failed (command missing, port closed, TLS error). The detail string
  in `natasha mcp health` carries the underlying error.

## Skill problems

```bash
natasha skills list
natasha skills validate ./my-skill      # manifest + entrypoint
natasha skills scan <id>                # static findings and undeclared capabilities
natasha skills test <id>                # sandbox test run
```

- `failed the static scan` is a refusal, not a warning: the skill's source contains something it did
  not declare (a subprocess, a socket, a credential path, `eval`, an obfuscated blob). Fix the skill or
  drop it.
- `the entrypoint printed nothing on stdout` means the skill process returned no JSON object. The
  contract is: read JSON on stdin, print one JSON object on stdout.
- A skill cannot be enabled until it is installed and approved; disabling it withdraws its tools
  immediately (`tests/integration/test_skills_chain.py` asserts this).

## Resetting safely

There is no "reset everything" button, deliberately. The supported options:

| you want | command |
| --- | --- |
| forget one memory | `natasha memory forget --id <id>` (soft) or `--hard` |
| stop a skill affecting turns | `natasha skills disable <id>` |
| start over with an empty home | point `NATASHA_HOME` at a new empty directory and run `natasha migrate up` |
| keep history but restore old data | stop the agent, restore a `natasha backup` archive |

Deleting the vault directory loses every credential permanently — that is the design, not an
accident. Back it up together with `keys/`.
