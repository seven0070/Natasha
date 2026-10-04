# Requirements matrix

Requirement by requirement, with the implementation, the runtime path, the test that proves it, and an
honest status. The commands and raw results behind these claims are in
[evidence.md](evidence.md).

**Legend (validation vocabulary).** `PASS` means implemented, wired into the runtime a user actually
drives, and covered by a test that would fail if the behaviour regressed. `PARTIAL` means implemented
but with a named gap — never "probably fine". `FAIL` would mean it does not work; nothing is `FAIL`
today. `UNVERIFIED` means implemented and tested as far as this environment allows, but the real
capability could not be exercised here (no model server, no display, no audio device, no network, no
browser); the qualifier `REAL-E2E: UNVERIFIED` marks exactly those rows. `DEFERRED` means the owner
explicitly postponed it (desktop). Publishing a marketplace package is the one feature that does not
exist at all, and it is named inside its `PARTIAL` row rather than hidden.

Last updated: 2026-10-04, by the full repository validation pass: **494 passed / 0 failed / 0 skipped**
(`python3 -m pytest --override-ini addopts="" -q`), `python3 scripts/check_architecture.py` → *218
modules, 698 internal imports, 0 violation(s), 30 declared exception(s)*, contract in sync. The
item-by-item evidence is in [validation-report.md](validation-report.md), and §D maps the 22 validation
steps to their results.

---

## A. Master-prompt requirements

| # | Requirement | Implementation | Runtime path | Test | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | Audit the repository into a requirement matrix | this file + [evidence.md](evidence.md); subsystem inventory from `natasha.runtime` | n/a | n/a | **PASS** |
| 2 | Finish every backend subsystem, wired to the real runtime | 27 subsystems attached by `NatashaRuntime.start()` (`backend/natasha/runtime.py`); a subsystem that cannot start is reported in `state.degraded`, never faked | every CLI command and API route goes through `get_runtime()` | `tests/integration/*` (13 files), `tests/e2e/*` | **PASS** (see §B for two PARTIAL subsystems) |
| 3 | Prove PERCEPTION→…→FINAL RESPONSE end to end | `natasha.executive.Executive.run_turn` / `stream_turn`; every stage writes an event | `POST /api/chat`, `POST /api/chat/stream`, `WS /api/chat/ws`, `natasha ask` | `tests/e2e/test_full_pipeline.py` (3 tests, walks all 14 stages and asserts evidence per stage), `tests/e2e/test_live_server.py::test_the_whole_owner_flow_works_over_http_and_survives_a_restart` | **PASS** |
| 4 | Polished ChatGPT-like UI (chat + 15 other screens) | `frontend/` (no build step): 16 views in `assets/js/views/`, streaming chat, attachments, mission + tool activity, approval cards, artifact preview | served by the API itself (`create_app(serve_ui=True)`) | `tests/integration/test_api_surface_chain.py::test_the_console_is_served_and_assets_exist`, `tests/e2e/test_live_server.py::test_the_console_is_served_from_the_same_origin` | **PARTIAL** — screens and streaming are implemented; **video input is upload-only** (no in-chat player) and the vision download path needs `api.js`'s blob helper |
| 5 | Local models: discovery, listing, capability detection, health, routing, streaming, fallback, local-only | `natasha.brain.adapters.{ollama,openai_compatible}` (+ `llamacpp`, `lmstudio`, `vllm` via the OpenAI-compatible adapter), `natasha.brain.registry`, `natasha.brain.router`, `brain.prefer_local` | `GET /api/providers`, `POST /api/providers/{name}/discover\|check\|enable`, `natasha providers …` | `tests/integration/test_provider_chain.py` (13), `tests/unit/test_provider_toggle.py` (5), `tests/unit/test_brain_fallback.py` (5), `tests/integration/test_api_surface_chain.py::test_discovery_of_an_unreachable_local_server_is_honest` | **PARTIAL — REAL-E2E: UNVERIFIED** — the code path is implemented and proven against a scripted transport; **no real local model was run here** (no Ollama/llama.cpp/LM Studio/vLLM, no outbound network). validation-report.md §23 carries the exact command to close it |
| 6 | Cloud providers: auth, discovery, streaming, errors, retries, fallback, rate limits, usage, no provider logic in the executive | `natasha.brain.adapters.*`, `natasha.brain.client` (retry/fallback), `natasha.brain.registry` (routing, usage tracker), credentials via the broker | `natasha providers add/enable/test`, the Providers screen | `tests/integration/test_provider_chain.py`, `tests/unit/test_brain_fallback.py`, `tests/unit/test_credentials.py` | **PARTIAL — REAL-E2E: UNVERIFIED** — adapters, retries, fallback, streaming and usage accounting are proven against scripted providers; **no live cloud call was made here** (no credentials, no outbound network). The offline `echo` provider is flagged (`offline_placeholder: true`), never passed off as a model |
| 7 | Computer use OBSERVE→PLAN→PERMISSION→ACT→OBSERVE→VERIFY across screen/mouse/keyboard/browser/terminal/filesystem/windows/apps, with risk-gated approvals | `natasha.computer` (controller, backends, accessibility tree), `natasha.tools.builtin` control tools, `natasha.security.policy` capabilities | `POST /api/computer/*`, computer tools in turns | `tests/integration/test_api_surface_chain.py` (capabilities, click/key/type refusals), `tests/security/test_permissions.py`, `tests/unit/test_policy.py` | **PARTIAL — REAL-E2E: UNVERIFIED** — policy, the central choke point, risk gating and the honest "no display/no backend" paths are proven (`test_perception_and_computer_chain.py` records a `ScreenBackend` and asserts the decision arrives **before** the backend runs); **live click/type/screenshot/keyboard/clipboard could not be exercised** (headless container, no X11/display) |
| 8 | Voice MIC→VAD→STT→NATASHA→TTS→SPEAKER with interruption/barge-in, local where practical | `natasha.perception.hearing` (VAD, STT adapters), `natasha.voice` (loop, wake word, TTS adapters) | `POST /api/voice/*`, the Voice screen, `natasha voice` | `tests/integration/test_api_surface_chain.py::test_voice_capabilities_and_loop_are_wired`, `…::test_a_text_voice_turn_runs_the_real_executive`, `…::test_an_empty_voice_turn_fails_with_a_reason`, `…::test_interrupt_reports_whether_something_was_playing` | **PARTIAL — REAL-E2E: UNVERIFIED** — the loop, barge-in handling, real VAD on generated WAV bytes and the honest failure paths are proven; **no microphone, speaker or STT/TTS engine exists here** (`available: false`, `speech_to_text: false`, with an install hint), so the acoustic path is unverified |
| 9 | Vision: image + screenshot understanding, OCR, UI interpretation, camera, video, accessibility tree — into the same runtime | `natasha.perception.vision` + `documents` (PDF/DOCX/XLSX/PPTX/CSV/image/zip), accessibility via `natasha.computer.accessibility` | `POST /api/vision/*` | `tests/integration/test_api_surface_chain.py` (9 tests: capabilities, OCR-honesty, document reader, accessibility honesty, UI interpretation, path refusal) | **PARTIAL — REAL-E2E: UNVERIFIED** — parsing and honest capability reporting are proven, including the refusal to describe a mislabelled non-image file (Pillow identifies the real format, empty when nothing matches); OCR/camera/video need optional extras and a device and report `available: false` with a reason here |
| 10 | Real MCP servers incl. malicious/untrusted tools | `natasha.mcp.client` (stdio + streamable HTTP), `natasha.mcp.registry` (discovery→inspect→approve→enable, namespaced tools, quarantine) | `POST /api/mcp/*`, `natasha mcp …` | `tests/integration/test_mcp_chain.py` (14 tests) against `tests/fixtures/mcp_echo_server.py` (a real subprocess) | **PASS** |
| 11 | Skills + marketplace full lifecycle | `natasha.skills.{manifest,models,store,lifecycle,isolation,runtime}`, `natasha.marketplace.{package,installer,registry}` | `natasha skills …`, `natasha marketplace …`, `POST /api/skills/*`, `POST /api/marketplace/*` | `tests/integration/test_skills_chain.py` (19), `tests/integration/test_api_skills_install.py` (5), `tests/integration/test_cli_skills.py` (4), `tests/unit/test_isolation.py` | **PARTIAL** — create→validate→scan→test→approve→install→activate→execute→update→rollback→disable→uninstall is **PASS** (19 + 5 + 4 tests, including a checksum mismatch and a scan-flagged skill being refused); **missing:** publishing/signing a package and a marketplace registry server (`packages/` holds the API contract, not a registry) |
| 12 | Real multimodal creation (text, images, audio, speech, video, code, documents, presentations, websites) with id/type/path/MIME/metadata/hash/provenance/verification status | `natasha.creation.engine` + `generators` (local deterministic generators + provider endpoints), artifacts via `natasha.memory.note_artifact` and the artifact store | `POST /api/creation/create`, `GET /api/creation/jobs/{id}`, `natasha create …` | `tests/integration/test_api_surface_chain.py::test_artifact_listing_and_content_hosting`; creation job paths asserted in `tests/e2e/test_live_server.py` | **PARTIAL — REAL-E2E: UNVERIFIED for images/video** — documents, code, presentations, websites and audio *speech* are produced by real local generators with id/type/MIME/hash/provenance/verification metadata; **images and video need a configured endpoint** (`creation.image_endpoint`) and are reported as such instead of fabricating bytes |
| 13 | Memory incl. restart persistence | `natasha.memory` (store, ten kinds, hybrid retrieval, working memory, provenance with trace ids) | `POST /api/memory`, `natasha memory …`, `remember`/`recall` tools | `tests/integration/test_memory_chain.py` (18, incl. unknown-kind → 422 on write, recall and listing), `tests/unit/test_memory_manager.py` (9), `tests/e2e/test_restart_persistence.py` (2) | **PASS** |
| 14 | Real missions (research→compare→report→save→show artifact) with failure/recovery | `natasha.missions.engine` + `supervisor` + `store`, `natasha.recovery`, `natasha.verification` | `POST /api/missions`, `natasha missions …`, Missions screen | `tests/integration/test_mission_chain.py` (10), `tests/integration/test_recovery_chain.py` (18), `tests/integration/test_verification_chain.py` (21), `tests/e2e/test_live_server.py` (mission → approval → resume → artifact → verify) | **PASS** |
| 15 | Verification: never "Done" without evidence; false success fails honestly | `natasha.verification.verify/require`, `executive.allow_unverified_success = false`, `VerificationEngine.require()` raising on unproven claims | every turn and mission | `tests/integration/test_verification_chain.py` (21), `tests/e2e/test_full_pipeline.py::test_a_failed_tool_is_repaired_once_and_never_reported_as_success` | **PASS** |
| 16 | Security audit: no credential leakage, no secrets in logs, no unauthorized execution, no privilege escalation, no sandbox escape, no approval bypass, no MCP/skill escalation, no injection governance bypass, no unrestricted fs/network | `natasha.security` (policy, validation, injection), `natasha.credentials` (vault + broker + log sanitiser), `natasha.approvals`, `natasha.governance` | every capability check, every tool call | `tests/security/test_governor.py` (19), `tests/security/test_permissions.py` (11), `tests/security/test_injection_matrix.py` (10), `tests/unit/test_credentials.py` (16), `tests/unit/test_injection.py` (6) | **PASS** for the tested properties (each is a named test, see evidence.md). Not a claim of formal verification — see "what would change this" in evidence.md |
| 17 | Upgrade Governor detect→propose→patch→test→benchmark→risk report→owner approval + adversarial tests | `natasha.governance.upgrade_governor` (+ `constitution`) | `GET/POST /api/evolution/*`, `natasha upgrades …`, Evolution screen | `tests/security/test_governor.py` (19 adversarial tests: self-approval, protected auto-apply, audit disabling, owner-authority removal, security weakening, constitutional edits, rollback removal) | **PASS** |
| 18 | DB/persistence: create, migrations, start/stop/restart, backup, recovery | `natasha.db` (schema, migrations), `natasha backup`, `natasha migrate` | `natasha migrate up\|down\|status`, `natasha backup`, container entry point | `tests/unit/test_db_migrations.py` (6), `tests/e2e/test_restart_persistence.py` | **PARTIAL** — the SQLite path (create/migrate/restart/backup/restore) is complete and tested; the **PostgreSQL schema and runner exist but the runtime stores are SQLite-only** (`memory.db_backend` is not honoured by the stores) |
| 19 | API: REST+WS, authn/authz, schema validation, rate limits, errors, audit, versioning, every sensitive endpoint authorized | `natasha.api` (38 routers incl. WS chat), `deps.require_owner`, `ratelimit`, uniform error envelope | `natasha serve` | `tests/integration/test_api_chain.py` (15), `test_api_surface_chain.py` (30), `test_rate_limits.py` (5), `test_api_skills_install.py` (5), `tests/unit/test_api_contract.py` (4) | **PASS** |
| 20 | Desktop app (Win/macOS/Linux, tray, notifications, permissions, autostart) | — | — | — | **DEFERRED** to repository phase 2 by explicit owner instruction (recorded 2026-10-04). The boundary is prepared: `tauri` is on the forbidden-import lists, the API contract is pinned in `packages/api-contract/`, and `docs/deployment.md` documents what the desktop shell will target |
| 21 | Deployment: Dockerfile, compose, env config, migrations, startup scripts, health checks, prod config, reproducible clean env | `Dockerfile`, `docker-compose.yml`, `.env.example`, `infra/docker/entrypoint.sh`, `infra/systemd/natasha.service`, `config/natasha.toml` | container entry point → `natasha migrate up` → `python -m apps.server` | `tests/unit/test_deployment_artifacts.py`, `tests/e2e/test_live_server.py` (the same startup path without docker) | **PARTIAL** — all artifacts exist and the configuration layers are tested; **the image build itself was not executed** (no Docker daemon in the verification environment). Command to close it is in evidence.md |
| 22 | Docs describing what actually exists | `docs/` (14 files) + `ARCHITECTURE_REFACTOR_REPORT.md` | n/a | `tests/unit/test_deployment_artifacts.py` checks referenced paths exist; every doc block in this session was executed before being written | **PASS** |
| 23 | Remove placeholders from production-critical paths | grep inventory over `backend/`, `apps/`, `scripts/`, `frontend/` — see evidence.md | n/a | n/a | **PASS** with an explicit inventory: 24 hits, all deliberate (abstract `NotImplementedError`, the *honest* offline placeholder, HTML input placeholders, temp-dir wording). No `TODO`/`FIXME` in reachable code |
| 24 | Full suite incl. every listed category; fix failures | `tests/`: unit 14 files/126 tests, integration 18/240, security 4/92, e2e 4/8, architecture 1/28 (`natasha_testkit.py` + fixtures) | `pytest` | `python3 -m pytest --override-ini addopts="" -q` → **494 passed, 0 failed, 0 skipped, 1 third-party warning in 51.8 s** (validation-report.md §3) | **PASS** |
| 25 | Clean-install production test: install→configure→start→authenticate→chat→local model→cloud model→memory→tool→MCP→skill→mission→artifact→verification→shutdown→restart | automated in `tests/e2e/test_live_server.py` (real server process, real HTTP, restart) + `tests/e2e/test_restart_persistence.py`; the manual sequence is in `docs/deployment.md` | — | `tests/e2e/test_live_server.py` (3), `tests/e2e/test_restart_persistence.py` (2) | **PARTIAL — REAL-E2E: UNVERIFIED** — every step is automated *except the two that need external services*: a real local model and a real cloud model, which cannot run in this offline environment (requirements 5/6). The rest of the sequence (install → start → authenticate → chat → memory → tool → MCP → skill → mission → artifact → verification → restart) was re-walked by the validation pass |
| 26 | Final matrix with evidence | this file | — | — | **PASS** |
| 27 | Declare "NATASHA — PRODUCTION COMPLETE" only when everything passes | not declared | — | — | **DEFERRED** — the declaration is not made. The validation pass classifies the repository as *validated with unverified capabilities*; the PARTIAL rows above name every gap and §C lists the remaining work |

---

## B. Subsystems

| Subsystem | Wired by | Status | Note |
| --- | --- | --- | --- |
| core (config, paths, clock, ids, errors, logging) | `runtime.py` | PASS | config layer precedence tested; env secret keys deliberately ignored |
| events (hash-chained log, sanitiser) | `runtime.py` | PASS | `verify_chain()` asserted in e2e and recovery tests |
| security (policy, validation, injection) | `runtime.py` | PASS | default deny; HIGH/CRITICAL need approval |
| governance (constitution, invariants, upgrade governor) | `runtime.py` | PASS | 19 adversarial tests |
| approvals (scoped, expiring, single-use) | `runtime.py` | PASS | fingerprint + TTL + consumption asserted |
| credentials (vault, broker, OAuth) | `runtime.py` | PASS | no plaintext, no logging of secrets; OAuth flows untested live (no provider) |
| memory (10 kinds, retrieval, working memory) | `runtime.py` | PASS | survives restart |
| world model | `runtime.py` | PASS | beliefs/entities/timeline tested; survives restart |
| brain (providers, models, routing, fallback, usage) | `runtime.py` | PARTIAL / UNVERIFIED | routing, fallback, streaming and usage proven with scripted providers; only `echo` here, flagged as the offline placeholder |
| perception (vision, documents, hearing) | `runtime.py` | PARTIAL / UNVERIFIED | parsing, refusal paths and honest capability reporting tested; OCR/camera/video need extras and devices |
| voice (loop, wake word, TTS) | `runtime.py` | PARTIAL / UNVERIFIED | loop, barge-in and real VAD tested; no audio device and no STT/TTS engine here |
| computer use (controller, backends, accessibility) | `runtime.py` | PARTIAL / UNVERIFIED | policy choke point and honest-no-display tested; live input injection unverified |
| tools (38 tools) | `runtime.py` | PASS | every risk level exercised |
| MCP | `runtime.py` | PASS | real stdio server in tests |
| integrations (REST, Slack, webhook, GitHub) | `runtime.py` | PARTIAL / UNVERIFIED | registry/credential-reference paths tested; live third-party calls need credentials and network |
| skills | `runtime.py` | PASS | full lifecycle |
| marketplace | `runtime.py` | PARTIAL | install/rollback pipeline PASS; publish/signing and a registry server do not exist |
| missions (engine, supervisor, store) | `runtime.py` | PASS | incl. approval pause/resume |
| agents (roles, workers, delegation) | `runtime.py` | PASS | delegation covered in mission/agent tests |
| creation | `runtime.py` | PARTIAL / UNVERIFIED | local generators real; image/video need a configured endpoint and say so |
| executive (loop, streaming, repairs) | `runtime.py` | PASS | 14-stage evidence |
| verification | `runtime.py` | PASS | false success refused |
| recovery | `runtime.py` | PASS | classifier + bounded repairs |
| affect | `runtime.py` | PASS | state moves through the real engine |
| observability (metrics, traces, health) | `runtime.py` | PASS | doctor/status read from it |
| db (schema, migrations) | `runtime.py` | PARTIAL / UNVERIFIED | SQLite complete from an empty home; Postgres schema+runner exist, runtime stores are SQLite-only |
| api + cli (presentation) | entry point | PASS | 494 tests; 3 entry points (CLI, server, `apps/server.py`); 152-path contract in sync |

---

## C. What is left, in priority order

*Updated by the validation pass: item 1 stands unchanged as the largest gap; item 2's gate is green
(0 violations, 30 declared exceptions); item 3 is documented as SQLite-only rather than half-wired;
item 4 is the only genuinely missing feature.*

1. **Run a real local model** (Ollama/llama.cpp/LM Studio/vLLM) and a real cloud provider against the
   adapter contract, then record the transcript in evidence.md. This is the single largest PARTIAL; it
   needs a machine with either the model server or network access, not more code.
2. **Extract the ports** listed in §7 of `ARCHITECTURE_REFACTOR_REPORT.md` and empty
   `MODULE_EXCEPTIONS` (21 entries, 30 edges). The gate (`--strict`) already exists.
3. **PostgreSQL stores**, or remove `memory.db_backend` until they exist. Today the setting is declared
   and not honoured by the stores, which is documented in three places but should be resolved one way
   or the other.
4. **Marketplace publishing** (`natasha marketplace publish` + a registry server) — currently
   verify-only.
5. **One application-service layer** for the API/CLI so routers stop touching stores directly (the
   presentation→data exceptions in the report).
6. **UI gaps**: in-chat video playback, the `api.js` blob helper for the vision download path, and a
   Projects view if it is wanted.
7. **Desktop (phase 2)** — tray, notifications, autostart, native permissions, packaging for
   Windows/macOS/Linux, per the owner's deferral.

Nothing above is blocked on the desktop work, and none of it is hidden behind a green check mark.

---

## D. Full repository validation (2026-10-04)

The 22 steps of the validation prompt, with what was run and what it returned. The narrative version,
including defects, limitations and the final classification, is
[validation-report.md](validation-report.md).

| Step | Evidence | Status |
| --- | --- | --- |
| 1 Inspect the repository | 287 tracked files, clean tree, 29 subsystems attached, `degraded: []`, 38 tools, 152 contract paths | **PASS** |
| 2 Run the suite | `python3 -m pytest --override-ini addopts="" -q` → 494 passed, 0 failed, 0 skipped, 1 third-party warning, 51.8 s | **PASS** |
| 3 Categorise every test | 40 files / 433 test functions / 494 collected; AST scan: 0 import-only, 0 assertion-free | **PASS** |
| 4 Verify the layering | `check_architecture.py` → 218 modules, 698 internal imports, 0 violations, 30 declared exceptions; forbidden-import sweep clean except the declared lazy `playwright` import in the infrastructure computer layer; presentation does no SQL/filesystem/provider work | **PASS** |
| 5 Prove the runtime path | `tests/e2e/test_full_pipeline.py` prints 12 stage lines (perception → memory → policy → execution → verification → repair → memory write → response → audited events) on the real stores/policy/approvals/log; only the provider is stubbed | **PASS** |
| 6 API validation | Contract in sync; 401 before login, 200 after; 404 for unknown ids, 400 for unknown skill, 422 for malformed bodies, 405 for `POST /api/chat/turn`; all 61 parameterless GETs probed, **0 5xx**; three memory-kind 500s found and fixed | **PASS after the fixes in §21 of the report** |
| 7 Voice | Capability states honest (no backends, `speech_to_text: false`); real VAD on WAV; STT and microphone refusals; acoustic path not exercisable here | **PARTIAL / UNVERIFIED** |
| 8 Perception | Vision MIME honesty fix, OCR availability gate, document readers, hearing transcription gate, accessibility tree | **PASS for the available paths; OCR/camera/video UNVERIFIED** |
| 9 Computer use | Controller honesty, central policy choke point proven to fire before the backend runs, browser http backend, SSRF refusal; live input injection impossible (no display) | **PARTIAL / UNVERIFIED** |
| 10 Security regression | 92 security tests: traversal/symlink/denied paths, owner-only and worker ceilings, scoped expiring single-use approvals, 32 injection cases, 33 governor adversaries, credential and log hygiene, process reaping | **PASS** |
| 11 MCP, skills, marketplace | 14 + 19 + 5 + 4 tests: hostile MCP output flagged, real stdio server, full skill lifecycle, marketplace install/rollback; publishing and a registry server do not exist | **PARTIAL** |
| 12 Memory and world model | 18 + 16 + 9 tests: ten kinds, hybrid retrieval, provenance, correction, restart survival, beliefs/relationships/merges; unknown kinds now answer 422 everywhere | **PASS** |
| 13 Missions | 10 mission + 18 recovery tests: plan → delegate → execute → verify → recover → complete, approval pause/resume, worker containment, rollback honesty | **PASS** |
| 14 Providers | Registry/routing/weights/retries/fallback/streaming/health/usage proven with scripted providers; `echo` flagged as the offline placeholder; real inference not exercisable | **PARTIAL / UNVERIFIED** |
| 15 Frontend | 6 syntax tests, 22 modules / 16 views, served same-origin from the API, UI endpoints contract-checked | **PARTIAL — BROWSER-E2E: UNVERIFIED** |
| 16 Migrations | From an empty home: `migrate up` applies 0001+0002, `migrate status` current 0002 / pending 0 / drift false, 18 tables; sqlite/postgres parity test-enforced; Postgres runtime stores unverified (no server) | **PARTIAL / UNVERIFIED** |
| 17 Configuration | Default deny, five `require_approval_for` entries, `prefer_local`, layered env loading, no committed credentials, no absolute developer paths | **PASS** |
| 18 Documentation | Docs inventory checked against the code; `export_openapi.py --check` in sync; `docs/api.md` references 25 of 152 contract paths (it is a curated guide) | **PARTIAL** (documentation coverage) |
| 19 Requirements matrix | this file, re-statused with the validation vocabulary | **PASS** |
| 20 Clean-repo check | Every required artifact tracked (pyproject, Dockerfile, compose, `.env.example`, `config/natasha.toml`, infra, apps, frontend, migrations); no ignored-but-required file; the suite runs from the repository itself | **PASS** |
| 21 No silent fixes | Six defects fixed at their cause (§21 of the report); no test deleted or weakened, no approval bypassed, no security disabled, no real code mocked | **PASS** |
| 22 Final report | [validation-report.md](validation-report.md) — 25 sections, ending in **REPOSITORY VALIDATED WITH UNVERIFIED CAPABILITIES** | **PASS** |
