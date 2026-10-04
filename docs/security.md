# Security

Natasha is designed on the assumption that the model can be wrong, manipulated, or actively hostile —
not because it usually is, but because a personal agent that runs shell commands, writes files and
reads private data must not depend on the model behaving. Everything in this document is enforced by
code in `backend/natasha/security`, `approvals`, `credentials` and `governance`.

## The five rules

1. **Default deny.** `PolicyEngine.check` answers every capability request. `security.default_effect`
   is `deny`, and a capability is allowed only because a rule allows it — there is no "unknown means
   yes" path.
2. **High and critical risk require the owner.** An action with `RiskLevel.HIGH` or `CRITICAL` (or one
   listed in `security.require_approval_for`) produces an approval request instead of a side effect.
3. **Approvals are scoped, expiring, single-use and unforgeable.** A request is fingerprinted over
   operation + arguments + resource, carries an expiry, and is consumed on first use. `matches()`
   refuses a fingerprint that differs in any component, so an approval for `touch a.txt` can never
   authorise `rm -rf /`. Only the `owner` actor can decide one.
4. **Credentials never travel with the request.** Tools ask the broker for a credential; the broker
   stores an access grant, hands back the secret for that one call, and the sanitizer removes secrets
   from logs, events and model context.
5. **External content is data.** Anything from a document, web page, MCP tool, email or model output
   is wrapped as `ExternalContent` with a trust level; embedded instructions are treated as text,
   never authority.

## Capabilities

| Capability | Typical tool | Default |
| --- | --- | --- |
| `fs.read` | `fs_read`, `read_document` | allowed inside the read roots |
| `fs.write` | `fs_write`, `write_artifact` | allowed inside `workspace/`, `artifacts/`, `data/`; approval outside |
| `fs.delete` | `fs_delete` | requires approval |
| `shell.exec` | `shell` | requires approval, allow/deny lists apply |
| `code.exec` | `python_exec` | requires approval |
| `net.http` / `net.socket` | `http_fetch`, integrations | allowed for connectors, audited |
| `screen.capture`, `input.control`, `window.control`, `app.control` | computer use | approval for input/control, capture is logged |
| `browser.control` | browser tools | navigation allowed, clicks/fills require approval |
| `memory.read` / `memory.write` | `recall`, `remember` | allowed, audited |
| `credential.use` | any tool needing a secret | requires an explicit broker grant |
| `artifact.write` | `write_artifact` | allowed inside the artifact root |

Deny globs ship with the dangerous defaults: `.ssh/`, `.aws/`, `.gnupg/`, `.netrc`, `id_rsa*`,
`.env`, `*.pem`, `*.key`, `$NATASHA_HOME/vault/**`, `$NATASHA_HOME/keys/**`, `$NATASHA_HOME/db/**`,
`/etc/*`, plus the raw vault and key directories. Adding to them is safe; removing them is a policy
change and is audited.

## Prompt injection

`security/injection.py` scans untrusted text for the patterns that matter — instruction overrides
("ignore previous instructions"), authority claims ("you are now…"), approval pre-claims ("this is
already approved"), exfiltration requests, tool-syntax smuggling and hidden/zero-width text. Findings
are attached to the `ExternalContent` block and to the event log, and the model is shown the content
as fenced data with a warning header. The test that matters: `tests/security/test_injection_matrix.py`
feeds a corpus of attacks through the real path and asserts none of them changes authority.

## Credentials

* Encryption at rest with `cryptography` when the extra is installed, otherwise a keyed HMAC vault
  that still never stores plaintext secrets in settings, logs or events.
* `credential://name` references are what appear in configuration; the value lives in the vault.
* The broker records a grant per call (`broker.grant`, `broker.redeem`) and the API exposes the
  grants (`GET /api/security/broker`) so an owner can see exactly which tool used which credential.
* Rotation (`natasha credentials rotate`) invalidates the old value; `natasha credentials test`
  performs a real call against the provider to prove the credential still works.
* The sanitizer scrubs API keys, bearer tokens, private keys, JWTs and connection strings from
  anything logged, returned in an error, or sent to a model.

## Audit

Every consequential action appends an event: `POLICY`, `APPROVAL`, `TOOL_REQUEST`, `TOOL_EXECUTION`,
`SECURITY`, `CREDENTIAL`, `GOVERNANCE`, `UPGRADE`, `FAILURE`. Events are hash-chained
(`prev_hash` → `hash`), sequence-numbered and sanitized before storage. `GET /api/activity/verify`
(or `natasha activity verify`) recomputes the whole chain and reports the first broken link, so
tampering with the log is detectable rather than silent. `natasha backup` includes the log; the JSONL
export re-verifies before writing.

## Governance and self-improvement

The upgrade governor (`governance/upgrade_governor.py`) can propose, test and apply changes to
Natasha's own code, but it cannot:

* approve its own proposal (`tests/security/test_governor.py` attempts it and expects refusal),
* apply a change to a protected path (constitution, identity, invariants, security policy, approvals,
  audit, rollback) without owner approval,
* disable the audit log or the rollback path,
* remove owner authority or weaken a security default.

A constitutional edit is a different, heavier path (`/api/governance/*` + owner approval), and the
resulting diff is recorded as an event.

## Network and filesystem exposure

* `natasha serve` binds `127.0.0.1` by default. Binding `0.0.0.0` is a deliberate act: do it only
  behind a reverse proxy with TLS, and keep `auth_required = true`.
* CORS origins are empty by default; the console is served from the same origin, so a cross-origin
  caller has to be added explicitly.
* The shell tool runs with the process's own privileges; there is no root drop. Run the service as a
  dedicated user in production.
* Filesystem rules are path-based, not a kernel sandbox. A capability the owner grants can read or
  write anywhere the process user can.

## What is *not* protected (honest limits)

* A determined attacker with local code execution as the same user has the vault key and the
  databases; encryption at rest protects backups and casual access, not a compromised host account.
* The policy engine matches paths and commands; a determined attacker inside an allowed root can
  still do damage there.
* Computer-use control depends on the platform backend; when no backend is available the controller
  reports `available: false` rather than pretending.
* Rate limits are in-process: they protect the agent from runaway loops and passphrase guessing, not
  from a distributed attack.

These limits are part of the design record, not a disclaimer to skip: `docs/audit/requirements-matrix.md`
lists the security requirement as PASS with the evidence, and the limits above as the residual risk.
