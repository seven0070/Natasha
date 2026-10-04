# Configuration

`config/natasha.toml` is the repository-level settings file. It is read from the checkout (or from
`$NATASHA_CONFIG`, which the container sets to `/app/config/natasha.toml`), and it is layered:

```
built-in defaults
  -> config/natasha.toml                     (this directory: deployment shape, shared by everyone)
     -> $NATASHA_HOME/config/natasha.toml    (machine-specific: paths, written by Settings.save)
        -> NATASHA_* environment variables   (nested keys use *two* underscores)
           -> explicit overrides in code
```

Every layer is optional: a missing file is not an error, and the built-in defaults stand. An invalid
TOML file *is* an error and raises `ConfigurationError` at load time rather than being ignored.

```bash
natasha doctor                                  # what the agent actually loaded, and what is degraded
natasha status                                  # host, port, deployment, features
NATASHA_EXECUTIVE__MAX_STEPS=80 natasha serve    # one-off override, no file edit
```

## What belongs in which layer

| layer | belongs there | does not belong there |
| --- | --- | --- |
| `config/natasha.toml` (here) | deployment shape: bind address, rate limits, model preference, feature switches, security posture | absolute paths, credentials, anything machine-specific |
| `$NATASHA_HOME/config/natasha.toml` | the machine's own settings - it is written by the agent and may contain paths under that home | credentials |
| environment | container/unit values that differ per deployment, and one-off experiments | credentials, long lists |

## Credentials are never configuration

Provider API keys, OAuth tokens, and any other secret live in the encrypted vault under
`$NATASHA_HOME/vault`, and tools receive them through the credential broker only for the call that
needs them. `natasha.core.config` ignores any environment variable whose first path segment is
`api_key`, `secret` or `token`, so a leaked shell environment cannot silently become the agent's
credentials. Add a key with:

```bash
natasha credentials add openai          # prompts, input is not echoed
natasha credentials list                # refs, never values
```

## Changing settings

- **From the CLI** the flags are per command (`--host`, `--port`, `--log-level`); to change what is
  persisted, use the console's Settings screen or edit the file in the layer that owns the value.
- **From the console** `PATCH /api/settings` accepts a nested object and writes it back to
  `$NATASHA_HOME/config/natasha.toml`. The fields that may be changed this way are listed in
  `natasha.api.routers.settings.MUTABLE`; anything else is refused rather than silently dropped.
- **Security-relevant keys are not casually changeable.** `security.default_effect`,
  `security.require_approval_for` and the governance invariants are protected: weakening them is a
  governance change with an audit trail, not a settings edit.
