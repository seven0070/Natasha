# Natasha — Layered Architecture Refactor Report

Status: **repository phase, complete for this pass** · branch `arena/01a105f9-natasha` ·
measured with `scripts/architecture_map.py`; the machine-readable baseline is committed at
`docs/evidence/architecture.json`

---

## 1. Executive summary

The repository already had a working subsystem layout, but "which module may import which" was
folklore: nothing in the code decided it, so nothing could fail when it was broken. This pass turned
the layering into **executable architecture** and then used it to find and fix real defects.

What changed:

| deliverable | what it is |
| --- | --- |
| `backend/natasha/architecture.py` | the single source of truth: 9 layers, 68 prefix entries, 61 directional rules, a deny-by-default rule table, per-layer forbidden modules, a reasoned exception list, **and the AST import scanner** that measures the repository |
| `scripts/architecture_map.py` | the report: layer matrix, every cross-layer import (`--edges`), machine-readable output (`--json`), exit code for CI (`--check`, `--strict`) |
| `scripts/check_architecture.py` | the CI gate: one line per violation, `--strict` also fails on the declared debt |
| `tests/architecture/test_dependency_rules.py` | 15 tests (28 cases) that enforce the same thing inside the suite, including "no module is unclassified", "no absolute-path imports", "business never imports a framework/driver/SDK", and "a declared exception may not go stale" |
| `backend/natasha/skills/{models,store}.py` | a real extraction: the SQLite registry moved out of the business-layer lifecycle into a data-layer store with a shared vocabulary in the interface core |
| `docs/architecture.md` | the layer table and the commands, next to the existing subsystem diagram |

Result of this pass, on the whole repository (203 modules, 660 project-internal imports):

```
architecture: 203 modules, 660 internal imports, 0 violation(s), 30 declared exception(s)
```

**Zero undeclared cross-layer imports.** The 30 remaining exception *edges* come from 21 declared
entries, each reasoned, live and ratcheted (§6) — each one names the port or move that removes it (§7). No rule was weakened
to make a component pass: where the code was wrong, the code changed (§5).

---

## 2. The layer model

Longest prefix wins, so a file-level entry can override its package. `natasha.architecture.layer_of`
classifies every module; `layer_table()` prints the grouping.

| layer | modules | may import |
| --- | --- | --- |
| `presentation` | `natasha.api.*`, `natasha.cli.*`, `frontend/` | presentation, business, ports, core |
| `business` | executive, brain (routing/loop), tools catalogue, missions, agents, memory rules, world, creation, skills lifecycle, marketplace, verification, recovery, affect | business, ports, core |
| `ports` | `natasha.tools.base`, `natasha.verification.checks`, `natasha.brain.adapters.base`, `natasha.skills.manifest`, `natasha.skills.models`, `natasha.memory.models`, `natasha.memory.embeddings`, `natasha.missions.models`, `natasha.world.models` | ports, core |
| `data` | `natasha.db`, all `*.store`, `natasha.governance.constitution`, `natasha.governance.upgrade_governor`, `natasha.approvals.engine`, `natasha.credentials.vault|manager`, `natasha.api.auth`, `natasha.mcp.registry`, `natasha.marketplace.registry`, `natasha.skills.store`, `natasha.affect.engine|state` | data, ports, core |
| `infrastructure` | `natasha.brain.adapters.*`, `natasha.mcp` (client/transport), `natasha.integrations`, `natasha.computer`, `natasha.voice`, `natasha.perception` (engines, parsers), `natasha.observability`, `natasha.creation.generators`, `natasha.skills.isolation`, `natasha.marketplace.package` | infrastructure, ports, data, core |
| `core` | `natasha.core`, `natasha.events`, `natasha.security`, `natasha.credentials` (broker), `natasha.approvals` (rules), `natasha.governance` (rules) | core, data, ports |
| `composition` | `natasha.runtime`, `apps/` | everything |
| `scripts`, `tests` | tooling and the suite | everything, deliberately |

Three properties make the model usable rather than decorative:

1. **Deny by default.** `rule_for()` returns `False` for any pair that is not in `RULES`, so a new
   dependency cannot slip in "because nobody wrote a rule against it".
2. **Not by layer alone.** `BUSINESS_FORBIDDEN_MODULES` and `PRESENTATION_FORBIDDEN_MODULES` stop
   business code from importing a web framework, a database driver, a browser/OS-control library or
   a provider SDK even when the target module is not part of the repository; the suite checks it on
   every third-party import.
3. **Debt is written down.** `MODULE_EXCEPTIONS` lists `(module, target layer, reason)`. The suite
   fails when an entry no longer matches an import, so the list can only shrink honestly.

The `ports` layer is deliberately broad: it holds the interfaces **and the domain vocabulary** that
both sides need (`SkillRecord`, `MemoryRecord`, `Mission`/`Step`, the check contract). Putting an
entity there is what lets a store and the rules above it share a type without one importing the
other — that is what removed the original `data -> business` violations.

---

## 3. How it is measured and enforced

```bash
python3 scripts/architecture_map.py                 # layer matrix + exceptions, human readable
python3 scripts/architecture_map.py --edges         # every cross-layer import with file:line
python3 scripts/architecture_map.py --json          # machine readable (the audit uses this)
python3 scripts/check_architecture.py               # CI gate, exit 1 on undeclared violations
python3 scripts/check_architecture.py --strict      # also fails on declared exceptions
python3 -m pytest tests/architecture -q             # the same rules, in the suite
```

The scanner (`natasha.architecture.scan_tree`) parses every `.py` under `backend/natasha`, `apps`,
`packages`, `scripts` and `tests` with `ast`, resolves relative imports against the **real module
set** — `from .runtime import X` inside `natasha/skills/__init__.py` is `natasha.skills.runtime`,
while the same statement inside `natasha/brain/adapters/__init__.py` may mean `natasha.brain.*` —
and records whether the import runs at module load or lazily inside a function.

Why that matters in practice: the first version of the resolver mis-resolved relative imports in
package `__init__.py` files, which both hid real edges and invented false ones. Fixing the resolver
changed the report and exposed `backend/natasha/brain/adapters/echo.py:62`, whose `from .... import
memory` had **one dot too many** — a latent `ImportError` on the local-embedding fallback path that
no test had reached. That is the kind of defect the measurement exists to find; the line is fixed.

---

## 4. Before → after

| measure | before this pass | after |
| --- | --- | --- |
| modules with a declared layer | 155 (`unclassified` for the rest) | **203**, none unclassified |
| internal imports measured | 227 (only top-level and class/function bodies) | **660** (every import, incl. inside `if`/`try`/`with`, tagged lazy) |
| undeclared cross-layer violations | 112 (raw), 16 after an accurate scan | **0** |
| declared exceptions | 0 written down | 21 entries (30 import edges), each with a reason and a planned fix |
| enforcement | none | CI gate + 15 suite tests |

The 16 violations the accurate scanner found, and what happened to each:

| violation | resolution |
| --- | --- |
| `brain/adapters/__init__` → `brain.{anthropic,base,echo,gemini,ollama,openai_compatible}` (7 edges) | **classification fix**: the concrete adapter modules were labelled business because they sit under `natasha.brain`. They are infrastructure; `brain/adapters/base.py` is the port. |
| `memory/store` → `memory/{models,embeddings,retrieval}`, `missions/store` → `missions/models`, `world/store` → `memory,world.models` (6 edges) | **classification fix**: entities and mechanics moved to `ports`/`data`. A store importing the entity it stores is not a layering error; a *store* importing *business decisions* is. |
| `affect/engine` → `affect/state` | **classification fix**: both halves are the affect table's persistence. |
| `missions/models` → `verification/checks` | **classification fix**: `VerificationCheck` is the check contract, so `verification/checks.py` is a port, not business. |
| `brain/client` → `memory/embeddings` | **classification fix** plus a declared exception for the factory's lazy `brain` import. |
| `skills/lifecycle` → `sqlite3` | **real extraction** (see §5). |

Two of the "violations" were genuine design questions rather than mislabels, and they are in §6:
`brain/registry` instantiating adapters, and `marketplace/installer` orchestrating the download/scan
pipeline and then writing to the registry.

---

## 5. Real defects found and fixed

1. **`from .... import memory` in `brain/adapters/echo.py`** — four dots from
   `natasha.brain.adapters.echo` resolves past the top of the package and raises `ImportError`. The
   local-embedding fallback in every adapter that used this pattern would have crashed the moment a
   provider without an embeddings endpoint was asked to embed. Fixed to `from ... import memory`, and
   proven by `PYTHONPATH=backend python3 -c "from natasha.memory import HashingEmbedder"`.
2. **SQLite inside the business layer.** `natasha/skills/lifecycle.py` opened `skills.db` itself —
   `sqlite3` is on the business forbidden list, and the lifecycle is a state machine that should not
   own a schema. The table, the `INSERT … ON CONFLICT`, the `get`/`list` queries and the connection
   handling moved to **`natasha/skills/store.py::SkillStore`** (data layer), the state enum and the
   record moved to **`natasha/skills/models.py`** (ports), and the lifecycle now takes an optional
   `store=` for tests. Behaviour is unchanged — the skill chain tests (`validate → scan → test →
   approve → install → activate → run → disable → uninstall`) pass untouched — but `sqlite3` is gone
   from every business module. `grep -rln sqlite3 backend/natasha --include=*.py` now lists only the
   stores and the authority modules that own their own schema (`db`, `events`, `approvals`,
   `credentials`, `governance`, `api.auth`, `mcp`, `marketplace`, `memory`, `missions`, `world`,
   `skills.store`, `affect`) plus the rule table itself.
3. **A stale-exception detector.** The first version of the exception list contained an entry for
   `natasha.skills -> composition` that no longer matched any import (it was an artefact of the
   resolver bug). `Report.stale_exceptions()` and a test now catch exactly this class of lie.
4. **The duplicate rule key.** `RULES` had `("core", "data")` twice (once allowed, once denied);
   the last one silently won. The rule table is now asserted to name each layer once per direction,
   and `core -> data` is the deliberate rule it was meant to be (core persists its own authority
   state: event log, approvals, constitution, vault, auth).

---

## 6. Remaining debt — 21 declared exceptions (30 import edges)

Each entry is `(module, target layer, reason)` in `natasha.architecture.MODULE_EXCEPTIONS`; the
file:line of every occurrence is printed by `scripts/architecture_map.py`. Grouped by the fix that
removes them:

### 6a. Ports not yet extracted (the bulk)

| module → | target | why it is accepted today | the fix |
| --- | --- | --- | --- |
| `memory.manager` | data | the façade drives its store | `MemoryRepository` port, store implements it |
| `missions.engine` | data | the engine drives its store | `MissionRepository` port |
| `marketplace.installer` | data, infrastructure | the installer orchestrates download/extract/scan and records the install | `PackageInspector` + `MarketplaceRepository` ports |
| `skills.lifecycle` | data, infrastructure | drives the skills store, the scanner and the sandbox | `SkillRepository` port (+ the scanner/sandbox already behind small classes) |
| `skills.runtime` | infrastructure | runs skills through the sandbox | `SkillSandbox` port injected via the tool context |
| `mcp.registry` | infrastructure | opens the client transport for a configured server | inject the transport factory |
| `tools.builtin` | infrastructure | the document tool calls the parsers | `DocumentReader` port on `ToolContext` |
| `brain.registry` | infrastructure | the registry instantiates adapters | adapter factory owned by the composition root |
| `memory.embeddings` | business | the provider-backed embedder factory | inject the brain instead of importing it |

### 6b. Presentation touching stores or engines directly

| module → | target | why | the fix |
| --- | --- | --- | --- |
| `api.app`, `api.routers.auth` | data | the app builds the auth store and the login endpoints are its HTTP face | an `AuthService` in business |
| `api.routers.governance` | data | endpoint reads constitution state | a governance service |
| `api.routers.mcp` | data | endpoint lists configured servers | an MCP service facade |
| `api.routers.{observability,system}` | infrastructure | the console exposes metrics/traces/doctor | read models behind a service |
| `api.routers.vision` | infrastructure | vision endpoints drive the perception engines | a perception service |
| `cli.main` | composition, data | the CLI is an entry point and its admin commands (migrate, mcp, governance, auth) drive stores directly | an application-service layer for admin commands |

### 6c. Genuine, intended edges

| module → | target | why it is allowed |
| --- | --- | --- |
| `api.app` | composition | the API entry point builds the runtime in its lifespan |
| `cli.main` | composition | the CLI composes a runtime per command |
| `governance.constitution`, `approvals.engine`, `credentials.vault` (classified data) | core | the authority layer owns its own persistence (see `core -> data` rule) |

The debt is 30 of 660 imports (4.5%), and the suite fails if it exceeds 10% — the architecture may
not quietly *become* the exceptions. Run `python3 scripts/architecture_map.py | tail -40` to print
every occurrence with its file, line and reason.

---

## 7. The ports plan (each item removes at least one exception)

1. **`natasha/ports/` as a home for the protocols.** `MemoryRepository`, `ArtifactRepository`,
   `MissionRepository`, `SkillRepository`, `MarketplaceRepository`, `ModelProvider`, `FileStorage`,
   `BrowserPort`, `SpeechToText`, `TextToSpeech`, `AccessibilityProvider`, `AuthService`,
   `DocumentReader`, `SkillSandbox`. The contracts exist in spirit today (they are used as the
   concrete stores' public surface); the move is to declare them as `Protocol`s and have the stores
   implement them, so business code imports an interface instead of an implementation.
2. **Inject, don't import.** The runtime (`composition`) constructs the concrete object and passes
   it to the subsystem constructor: `MissionEngine(store=…)`, `SkillLifecycle(store=…)`,
   `MCPServers(transport_factory=…)`, `ProviderRegistry(adapter_factory=…)`,
   `get_embedder(brain=…)`, `ToolContext(document_reader=…)`. Most constructors already take
   keyword-only collaborators, so this is additive.
3. **One application-service layer for the API/CLI.** Thin services in `business` (`GovernanceService`,
   `MCPService`, `ObservabilityService`, `VisionService`, `MemoryService`) that the routers and CLI
   commands call. This removes every `presentation -> data|infrastructure` exception without moving
   an endpoint's behaviour.
4. **Then flip the gate:** `scripts/check_architecture.py --strict` becomes the CI default and
   `MODULE_EXCEPTIONS` empties. The test suite's staleness check makes that a mechanical sequence,
   not a rewrite.

Ordering by value: (2) for memory/missions/skills is the smallest change with the largest effect;
(3) is mechanical; (1) is mostly moving declarations.

---

## 8. What this pass did **not** do

- **No directories were moved.** The layers are a *classification* over the existing layout, and the
  scanner proves every module has one. Physically restructuring `backend/natasha/` into
  `presentation/`, `business/`, … was explicitly not attempted: it would invalidate every import,
  every test and every documented path for no behavioural gain, and the user's standing rule is to
  reuse and stabilise what works before reshaping it.
- **The ports are planned, not built** — except `SkillStore`/`SkillRecord`, which are real (§5).
  §6 lists exactly which exception each remaining port removes, so the work is scheduled rather than
  hand-waved.
- **Desktop/Tauri is deferred** (repository phase 2) and appears in the audit matrix as such. Nothing
  in this refactor depends on it; `tauri` is on the forbidden-module lists so the boundary is ready
  when the desktop app arrives.
- **No rule was relaxed to make a component pass.** Where the layering was inconvenient, either the
  classification was wrong (fixed), the code was wrong (fixed), or the edge was written into
  `MODULE_EXCEPTIONS` with the reason and the fix.

---

## 9. Test evidence

```
$ python3 scripts/architecture_map.py
Layers: business, composition, core, data, infrastructure, ports, presentation, scripts, tests (9)
        over 203 modules | 660 project-internal imports | 30 declared exception(s)
...
no undeclared layering violations (203 modules scanned)

$ python3 scripts/check_architecture.py
architecture: 203 modules, 660 internal imports, 0 violation(s), 30 declared exception(s)
exit=0

$ python3 -m pytest tests/architecture -q
28 passed

$ python3 -m pytest tests -p no:randomly -q
421 passed
```

The architecture tests are:

1. every declared layer has a within-the-layer rule that allows it;
2. no rule names a layer that does not exist;
3. unknown pairs are denied by default;
4. the authority layer may not depend on behaviour, clients or adapters;
5. composition may reach every runtime layer, and no runtime layer may reach composition;
6. presentation may not reach a store or an adapter;
7. every module in the repository declares a layer;
8. the repository has zero undeclared violations;
9. twelve important modules sit in the layer they claim (executive→business, security→core,
   runtime→composition, adapters→infrastructure, …);
10. the scan actually saw the repository (a scanner that silently finds nothing cannot pass);
11. every relative import resolves to a module that exists;
12. every declared exception is live and carries a real reason;
13. the debt stays under 10% of the graph;
14. business code imports no framework/driver/SDK; presentation owns no infrastructure;
15. no module imports project code through the checkout layout (`from backend.natasha…`).

---

## 10. How to keep it green

- Run `python3 scripts/check_architecture.py` before every commit that touches imports; it takes
  under a second and reads no state.
- When a new module appears, add it to `_PREFIXES` in `backend/natasha/architecture.py` in the same
  commit — `tests/architecture` fails while it is unclassified, so this cannot be forgotten.
- When a new edge is genuinely needed, add it to `RULES` with a reason (allowed) or to
  `MODULE_EXCEPTIONS` with a reason and the planned fix (accepted debt). Never add a prefix just to
  make one import pass.
- When a port lands, delete the exception it replaces. The suite will not let a dead entry linger:
  a stale exception fails `test_every_declared_exception_is_live_and_carries_a_reason`.
- The report, the docs (`docs/architecture.md`) and the tests all read the same tables — if they ever
  disagree, the code in `backend/natasha/architecture.py` wins and the prose is the bug.
