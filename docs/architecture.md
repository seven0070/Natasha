# Architecture

Natasha is a composition of 27 subsystems under `backend/natasha/`, wired by one object
(`natasha.runtime.NatashaRuntime`) and driven by one loop (`natasha.executive.Executive`). The model
is a component: it proposes, the code decides.

## Layering

```
                ┌──────────────────────────────────────────────────────────────┐
  clients       │  Web console (frontend/)   CLI (natasha.cli)   REST/WS API  │
                └───────────────┬──────────────────────────────────────────────┘
                                │  auth · schema validation · rate limits · audit
                ┌───────────────▼──────────────────────────────────────────────┐
  executive     │  Executive: context → plan → capability → policy → approval  │
                │  → execute → observe → verify → repair → remember → answer   │
                └───┬────────────┬───────────┬───────────┬────────────┬─────────┘
                    │            │           │           │            │
  capability   brain/router   tool registry  MCP      agents      missions
  providers                  integrations  skills   supervisor    creation
                    │            │           │           │            │
  state        memory   world model  approvals  credentials  verification
               events (hash-chained)   observability   recovery
                    │
  authority    security.policy (default deny) · governance (constitution, invariants)
               approvals (scoped, expiring, unforgeable) · upgrade governor
                    │
  substrate    core (paths, config, ids, clock, errors) · db (schema, migrations)
```

## Authority boundary

Three rules are enforced in code, not in prompts:

1. **Default deny.** `security.policy.PolicyEngine` checks every capability request
   (`fs.read`, `fs.write`, `shell.exec`, `model.call`, `computer.control`, `credential.use`, …).
   A capability that is not explicitly allowed is refused with `PolicyDenied`.
2. **Approvals are explicit, scoped and consumed.** A high/critical action produces an approval
   request bound to operation + arguments + resource, with an expiry and a fingerprint. The owner
   approves once; the grant is consumed on first use (`approvals/engine.py`). Nothing about an
   approval can be asserted by the model, and no actor other than the owner can decide one.
3. **The constitution is not writable by the system.** `governance/` holds identity, invariants and
   protected paths. The upgrade governor cannot edit them, cannot self-approve, and cannot disable
   the audit log — `tests/security/test_governor.py` attacks those paths deliberately.

## The request pipeline

A turn runs through these stages, each leaving evidence in the event log:

| Stage | Code |
| --- | --- |
| perception | `Turn` accepted; attachments/documents/vision are **data** (never authority) |
| intent | turn classification, affect note (`affect/`) |
| memory retrieval | `executive/context.py` → `memory.store.recall` (hybrid: semantic, keyword, entity, recency, task, importance) |
| workspace | working memory (`memory/working.py`) gets the turn and the tool activity |
| planning | the brain is asked with the available tool schemas |
| capability selection | `tools/registry.py` resolves the tool the model named; unknown names fail closed |
| policy | `PolicyEngine.check` per tool capability |
| approval | `approvals/engine.py` when the policy requires one (nothing happens before a decision) |
| execution | tool / model / agent / MCP / skill — through the registry choke point |
| observation | output is wrapped as `ExternalContent` with a trust level and injection findings |
| verification | `verification/` checks; a failed claim is reported as failed |
| repair | `recovery/` bounded retries with a "try less" transformation, never a blind replay |
| memory update | tool writes are reported as `memory_writes`; the audit event carries the trace id |
| final response | `_final_text` marks offline placeholders instead of dressing them as answers |

`tests/e2e/test_full_pipeline.py` walks exactly this list and asserts the artefact each stage leaves.

## Storage

| Store | File | Contents |
| --- | --- | --- |
| event log | `db/events.db` | append-only, hash-chained, sanitized payloads |
| memory | `db/memory` | records + embeddings + provenance + history |
| missions | `db/missions.db` | missions, steps, artifacts, verification |
| approvals | `db/approvals.db` | requests, decisions, fingerprints, expiry |
| auth | `db/auth.db` | owner credential (PBKDF2), sessions |
| vault | `vault/` + `keys/` | encrypted credentials (never plaintext, never in settings) |
| world model | `db/world.db` | entities, relations, observations |

Migrations are generated from the declared schema (`natasha.db.schema`) with
`scripts/generate_migrations.py` and applied with `natasha migrate up`. The runner verifies
checksums, so an edited applied migration is reported rather than silently trusted.

## Subsystem map

| Package | Responsibility |
| --- | --- |
| `core` | paths, layered config, ids, clock, hashing, typed errors |
| `events` | hash-chained log, sanitizer, chain verification, JSONL export |
| `security` | policy engine, capabilities, injection defence, validation, secret redaction |
| `governance` | constitution, identity, invariants, upgrade governor, rollback |
| `approvals` | scoped/expiring approvals, decisions, mission re-arm |
| `credentials` | encrypted vault, broker (credentials only reach tools through it), OAuth flows |
| `memory`, `world` | 10 memory kinds, hybrid retrieval, decay/consolidation, entity graph |
| `brain` | provider adapters, model registry, routing with privacy tiers, fallback, usage |
| `perception` | vision (images, screenshots, OCR, camera, video), hearing, documents |
| `voice` | STT/TTS engines, conversation loop, wake words, barge-in |
| `computer` | screen/mouse/keyboard/window control, browser control, capability report |
| `tools` | registry, schemas, risk levels, artifacts, sandboxed execution |
| `mcp`, `integrations` | MCP client with tool quarantine; REST/Slack/GitHub/email connectors |
| `skills`, `marketplace` | skill lifecycle + publisher pipeline (scan, sign, install) |
| `agents`, `missions` | supervisor/worker fleet, durable missions with verification and rollback |
| `creation`, `affect` | multimodal generation jobs; affective state |
| `executive` | the loop above: context, run, stream, approvals, missions |
| `verification`, `recovery` | check library + engine; failure classification and bounded retries |
| `observability` | health monitor, metrics, tracing, usage |
| `api`, `cli` | REST + WebSocket surface; the command line |
| `db` | schema declaration, migration runner, backups |

## Extensibility

* **A new tool** subclasses `tools.base.Tool`, declares capability + risk + schema, and is
  registered — policy, approvals, auditing and trust fencing then apply automatically.
* **A new provider** is either config-only (any OpenAI-compatible endpoint) or an adapter
  subclass in `brain/adapters/`. The executive never contains provider-specific logic.
* **A new subsystem** is a package that the runtime attaches in `_attach` order and that exposes a
  reset hook, so tests can isolate it.

## Degradation instead of failure

`NatashaRuntime._attach` records what it could wire and what it could not. A missing optional
dependency (a speech engine, a display server, a local model server) marks that subsystem
unavailable and the API reports it honestly — the rest of the system keeps working. `natasha doctor`
and `GET /api/doctor` print that picture.
