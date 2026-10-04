# Natasha documentation

Natasha is a local-first personal agent: a Python backend (27 subsystems), a REST/WebSocket API, a
zero-build web console, and a test suite that drives the real runtime. This directory documents what
the code actually does — not what it is supposed to do one day. Where something is incomplete it is
labelled as such.

| Document | What it answers |
| --- | --- |
| [install.md](install.md) | How to install, start, and set up the owner on a clean machine |
| [configuration.md](configuration.md) | Every setting, where it lives, what may be changed at runtime |
| [architecture.md](architecture.md) | The subsystems, the request pipeline, and the authority boundary |
| [api.md](api.md) | REST + WebSocket surface, authentication, errors, examples |
| [security.md](security.md) | Default-deny policy, approvals, credentials, injection defence, audit chain |
| [providers.md](providers.md) | Cloud model providers: registration, routing, fallback, usage |
| [local-models.md](local-models.md) | Ollama, llama.cpp/GGUF, LM Studio, vLLM and local-only mode |
| [mcp.md](mcp.md) | Model Context Protocol servers, trust levels, tool quarantine |
| [skills.md](skills.md) | The skill lifecycle from creation to uninstall |
| [marketplace.md](marketplace.md) | Publishing, scanning and installing skills |
| [deployment.md](deployment.md) | Docker, configuration, migrations, health checks, backup, systemd |
| [troubleshooting.md](troubleshooting.md) | Reading `doctor`, the common failures, and how to recover |
| [developer.md](developer.md) | Repository layout, how to add a tool/provider/migration, test conventions |
| [audit/requirements-matrix.md](audit/requirements-matrix.md) | Requirement-by-requirement status with evidence |
| [audit/evidence.md](audit/evidence.md) | Commands and results that back the claims above |

Two rules the whole project follows, and which this documentation must not contradict:

1. **No claim without evidence.** A feature is described as working only when a test or a recorded
   command exercised it. `docs/audit/` is where that evidence lives.
2. **The model is never the authority.** Permissions, credentials, approvals, sandboxing and
   protected upgrades are enforced by code outside the model's reasoning. Documentation never
   promises a capability that bypasses that boundary.
