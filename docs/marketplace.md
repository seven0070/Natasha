# Marketplace

The marketplace distributes **skills** — signed packages of code that the agent can run — and it is
built around one assumption: a package is untrusted input until the owner has seen exactly what it
asks for. Installing something is a privilege decision, not a download.

Everything here is also a library: `natasha.marketplace` (pipeline), `natasha.skills` (lifecycle and
sandbox). The CLI and the API are two faces over the same code.

- [The package format](#the-package-format)
- [The install pipeline](#the-install-pipeline)
- [Install, run, update, roll back](#install-run-update-roll-back)
- [Publishing and trust](#publishing-and-trust)
- [What the scanner does and does not catch](#what-the-scanner-does-and-does-not-catch)
- [Where things live on disk](#where-things-live-on-disk)

## The package format

A package is a directory (or an archive of one) containing:

```
my-skill/
  skill.json      # manifest: id, name, version, entrypoint, permissions, risk, publisher…
  main.py         # the entrypoint file (any relative path named by the manifest)
  …               # assets, helper modules, data files
  manifest.json   # optional publisher signature material (publisher, signature, checksum)
```

`skill.json` is validated by `natasha.skills.manifest.validate_manifest`:

| field | rule |
| --- | --- |
| `id` | required, `^[a-z][a-z0-9_\-]{2,48}$` |
| `name` | required, human-readable |
| `version` | required, semver (`1.4.2`); versions are separate install directories |
| `entrypoint` | a **relative file path** inside the package (default `main.py`). No absolute paths, no `..`, no `module:function` form |
| `permissions` | the capabilities the skill needs (`fs.read`, `fs.write`, `net.http`, `memory.read`, `memory.write`, `shell.exec`, `code.exec`, `credential.use`) |
| `risk` | `LOW` / `MEDIUM` / `HIGH` / `CRITICAL`; drives the review the owner sees |
| `dependencies` | third-party Python packages the skill expects to be installed; checked against the environment before install |
| `publisher`, `checksum`, `signature` | optional provenance; checked when the publisher is trusted |

The entrypoint is executed **out of process**, with a scrubbed environment. It reads one JSON object
from stdin and prints one JSON object on stdout — that is the entire contract
(`natasha.skills.isolation`). A skill that prints nothing is reported as printing nothing, never as an
empty success.

## The install pipeline

`natasha.marketplace.installer` runs these stages, in order, and stops at the first failure:

| # | stage | what it proves |
| --- | --- | --- |
| 1 | **resolve** | the source is a local path or an `http(s)://` URL and exists (a missing path is a refusal, not an empty package) |
| 2 | **download** | the archive is fetched with a timeout and a size cap (local paths skip this) |
| 3 | **extract** | the archive unpacks inside a staging directory — the extractor refuses absolute paths and `..` traversals (zip-slip) |
| 4 | **validate** | the manifest parses, the entrypoint exists inside the package, the version is semver |
| 5 | **checksum** | a deterministic hash over the manifest + entrypoint + assets is recorded for the review and for post-install tamper checks |
| 6 | **publisher** | if a publisher is declared, it must be in `marketplace.trusted_publishers` (or the signature must verify) |
| 7 | **static scan** | the AST scan below; findings block the install |
| 8 | **dependency scan** | declared dependencies are checked against what is installed; missing ones are reported, not silently ignored |
| 9 | **permission review** | the requested capabilities are mapped to policy capabilities and shown to the owner |
| 10 | **owner approval** | a `HIGH`/`CRITICAL` package needs an explicit, scoped approval; the installer cannot approve itself |
| 11 | **install** | files land under `$NATASHA_HOME/skills/<id>/<version>/` with the checksum recorded |
| 12 | **activate** | the skill is registered, its tools become callable, and each call is policy-checked and audited |

Every stage is appended to `InstallPlan`/`InstallStep` records, so the CLI and the console can show
exactly which step refused and why. Nothing is installed "because it succeeded once" — an install that
fails at stage 7 leaves no partial skill behind.

## Install, run, update, roll back

```bash
# Inspect without installing anything (scan + permission review, no filesystem changes)
natasha marketplace review ./my-skill

# Install and activate (asks for the owner's agreement to the declared permissions)
natasha marketplace install ./my-skill --agree

# From a URL (size-capped at 200 MiB, checksum verified when one is given)
natasha marketplace install https://example.invalid/hello-1.2.0.zip --agree

natasha marketplace installed          # what is installed, at which versions, in which state
natasha skills run hello --payload '{"name": "owner"}'
natasha marketplace rollback hello --version 1.1.0     # a previous version is still on disk
natasha marketplace uninstall hello [--purge]          # unregister, and optionally delete the files
```

Versions are **separate directories** and separate registry rows (`id`, `version`), so an upgrade is
an install followed by an activation, and a rollback re-activates the older row. Both directions are
audited (`skill.installed`, `skill.activated`, `skill.disabled`, `skill.uninstalled` in the event log).
Disabling withdraws the skill's tools immediately — an uninstalled skill cannot stay callable.

Over REST the same operations are `GET /api/marketplace` (installed + stats),
`POST /api/marketplace/review`, `POST /api/marketplace/install`, `POST /api/marketplace/uninstall`,
`POST /api/marketplace/rollback`, `GET|POST /api/marketplace/sources`, plus the skill endpoints
(`POST /api/skills/install`, `POST /api/skills/{id}/enable|disable`, …) — see [api.md](api.md).

## Trust, checksums and signatures

The installer is a **verifier**, not a publisher: it validates provenance that the package carries.

- `expected_checksum` (CLI `--checksum` is not exposed yet; the API accepts it) is compared against the
  package's real checksum, so a package that changed in transit is refused;
- a declared `publisher` must appear in the installer's trusted-publisher list, otherwise the install
  is refused with a message telling the operator to verify the publisher first. That list is set where
  the installer is constructed (`MarketplaceInstaller(trusted_publishers=[...])`); there is **no
  settings key for it yet**, which is recorded honestly in the audit matrix;
- a `signature` over the checksum is verified when a publisher secret is configured for the installer;
- after install, the recorded checksum is compared again before activation: a skill modified on disk
  fails with "was modified after install; reinstall before activating".

**Not implemented:** publishing/signing *packages* (an upload endpoint or `natasha marketplace publish`
command) and a marketplace registry server. Sources are local paths or HTTPS URLs; named sources can be
registered (`POST /api/marketplace/sources`) and are stored for reference, but nothing in the agent
fetches from a catalogue by name yet. That is deliberate for now — a registry is infrastructure, and the
security pipeline (scan → review → approve → install) does not depend on one.

## What the scanner does and does not catch

`natasha.marketplace.package.static_scan` parses every `.py` file (a syntax error is itself a finding,
`unparseable`) and reports **evidence**, not keywords:

| pattern | raised when |
| --- | --- |
| `subprocess_shell` | a real `subprocess`/`os.system`-style call **with** `shell=True`, or an argv built from a shell string |
| `raw_socket` | `socket`, `http.client`, `urllib.request`, `requests`, `httpx` — a network call |
| `env_exfil` | an environment read **and** a network call in the same file |
| `credential_paths` | a string literal pointing at `.ssh`, `.aws`, `.gnupg`, `id_rsa`, `.env`, `…/vault/` |
| `eval_exec` | `eval`, `exec`, `compile`, `pickle.loads`, `marshal.loads` |
| `obfuscated_payload` | a string literal ≥ 60 characters that looks base64/hex-encoded |

Non-Python files (shell scripts, notebooks, JSON) are matched against a regex table with the same
pattern names rather than being parsed; a `.py` file that does not parse is reported as `unparseable`,
which blocks the install like any other finding.

Findings map to the capability they imply (`subprocess_shell → shell.exec`,
`raw_socket`/`env_exfil → net.http`, `credential_paths → credential.use`,
`eval_exec`/`obfuscated_payload → code.exec`), and a capability that is used but not *declared* is
called out in the review. The same scanner runs for local skills and marketplace packages, so the two
can never disagree about what a file does.

What it does not catch, and does not claim to: logic that is malicious without using a flagged
primitive, a dependency that turns malicious later, or a skill that exploits an *allowed* capability
within its declaration. That is why the permissions are shown to a human, why each call is
policy-checked at runtime, and why skills run out of process with a scrubbed environment. Static
analysis raises the cost of a sloppy or lazy attack; the runtime gate is what stops it.

## Where things live on disk

| path | contents |
| --- | --- |
| `$NATASHA_HOME/skills/<id>/<version>/` | the installed package files |
| `$NATASHA_HOME/db/skills.db` | install records and lifecycle state (`SkillStore`) |
| `$NATASHA_HOME/marketplace/` | staging, downloaded archives and the marketplace registry |
| `$NATASHA_HOME/db/natasha.db` | the event log entry for every install/activation/removal |
