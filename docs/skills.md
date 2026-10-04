# Skills

A skill is a small, versioned capability package: a manifest plus Python entry points that Natasha
can run in-process under policy. Skills are how the agent gains a new ability without patching the
core — and the lifecycle exists so that gaining an ability is always a reviewed act.

## Anatomy of a skill

```
skills/<skill_id>/
  skill.json          # manifest: id, version, description, entrypoint, permissions, tools
  <package>/          # the implementation
  tests/              # the author's own tests (run by `validate`/`test`)
```

The manifest declares, at minimum, the id and version, plus the **capabilities** the skill wants.
Those declarations are what the policy engine checks: a skill that declares `fs.read` gets a read
scope, and a skill that declares nothing gets nothing.

## Lifecycle

| Stage | CLI | API | What it proves |
| --- | --- | --- | --- |
| create | author a folder | `POST /api/skills` (via install of a local path) | — |
| validate | `natasha skills validate <path>` | `GET /api/skills/{id}/scan` | the manifest parses and the entrypoint exists |
| scan | `natasha skills scan <path>` | `GET /api/skills/{id}/scan` | static analysis: network/filesystem/subprocess use, obfuscation, dangerous imports |
| test | `natasha skills test <path>` | `POST /api/skills/{id}/test` | the skill's own tests, run in a subprocess with a timeout |
| approve | owner decision | approvals endpoint | a scanned skill still needs a human yes |
| install | `natasha skills install <path>` | `POST /api/skills/install` | copied into `$NATASHA_HOME/skills/<id>/<version>` |
| activate | `natasha skills enable <id>` | `POST /api/skills/{id}/enable` | its tools/entrypoints become reachable |
| execute | `natasha skills run <id> <entry> k=v` | `POST /api/skills/{id}/run` | runs through the tool choke point, under policy |
| update | install a new version | same install path | a new version is a new artifact, not a mutation |
| rollback | `natasha skills rollback <id> --to-version X` | `POST /api/skills/{id}/rollback` | the previous version can be restored |
| disable | `natasha skills disable <id>` | `POST /api/skills/{id}/disable` | stops routing to it without deleting it |
| uninstall | `natasha skills uninstall <id>` | `DELETE /api/skills/{id}` | files removed (or kept with `purge=false`) |

`natasha skills list` shows state, version and description for each skill; the console's Skills screen
shows the same thing.

## Security

* **Scanning is static and honest.** The scanner reports what it found (imports, URLs, subprocess
  calls, `eval`, encoded blobs) with locations, and refuses a package with a manifest it cannot parse.
* **Permissions come from the manifest, not from the code.** A skill cannot grant itself a capability
  at runtime; the runtime that executes skill code resolves each call through `PolicyEngine`.
* **No self-approval.** Installing a fresh skill whose manifest requests high-risk capabilities
  produces an approval request; the skill runtime cannot approve it.
* **Test isolation.** `test` runs the skill's tests in a subprocess with a timeout and a scratch home,
  so a skill cannot mutate the live database by being tested.
* **Rollback is real.** Versions are separate directories; rollback re-activates the previous one and
  leaves the newer version on disk for inspection.

## Writing a skill

```python
# skills/hello/hello/__init__.py
def run(arguments: dict, context) -> dict:
    """Entry point: return a plain dict; raise to fail honestly."""
    return {"greeting": f"hello {arguments.get('name', 'world')}"}
```

```json
{
  "id": "hello",
  "version": "1.0.0",
  "description": "Say hello",
  "entrypoint": "hello:run",
  "permissions": [],
  "tools": [{"name": "hello_say", "description": "Say hello", "capability": "memory.read",
             "risk": "LOW", "schema": {"type": "object", "properties": {"name": {"type": "string"}}}}]
}
```

Then:

```bash
natasha skills scan skills/hello
natasha skills test skills/hello
natasha skills install skills/hello
natasha skills run hello run name=owner
```

## Limitations

* Skills run in-process (not in a separate sandbox process). They are subject to policy, approvals and
  auditing, but a malicious skill that gets past the owner's review shares the process's privileges.
  That is why the scanner, the manifest permissions and the explicit approval exist.
* Distribution is via the marketplace ([marketplace.md](marketplace.md)); a skill installed from a
  path runs the code in that path.
