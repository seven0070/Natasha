"""Command line entry point: ``natasha <command>``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

CLI_ACTOR = "local-owner"

BANNER = r"""
  _   _       _       _
 | \ | | __ _| |_ ___| |__   __ _
 |  \| |/ _` | __/ __| '_ \ / _` |   local-first personal agent
 | |\  | (_| | || (__| | | | (_| |
 |_| \_|\__,_|\__\___|_| |_|\__,_|   v{version} ({codename})
"""


# --------------------------------------------------------------------------- helpers
def _print(value: Any, *, as_json: bool = False) -> None:
    if as_json or not isinstance(value, (str, int, float, bool, type(None))):
        print(json.dumps(value, indent=2, default=str))
    else:
        print(value)


def _runtime(**kwargs: Any) -> Any:
    from ..runtime import get_runtime

    return get_runtime(**kwargs)


def _bounded(text: str, width: int = 100) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the API + UI server."""
    import uvicorn

    settings = _runtime().settings
    host = args.host or settings.host
    port = args.port or settings.port
    print(BANNER.format(version=_version(), codename=_codename()))
    print(f"Listening on http://{host}:{port}  (NATASHA_HOME={settings.data_dir})")
    if host not in ("127.0.0.1", "localhost", "::1") and settings.auth_required:
        print("WARNING: binding to a non-loopback address; owner authentication is required for every route.")
    uvicorn.run("natasha.api.app:get_app", factory=True, host=host, port=port, log_level=args.log_level)
    return 0


def _version() -> str:
    from .. import __version__

    return __version__


def _codename() -> str:
    from .. import CODENAME

    return CODENAME


def cmd_chat(args: argparse.Namespace) -> int:
    """Interactive chat (streaming) with the executive."""
    import asyncio

    from ..core import run_coroutine_sync
    from ..executive import Turn

    runtime = _runtime()
    conversation = args.conversation or f"cli-{os.getpid()}"
    print(BANNER.format(version=_version(), codename=_codename()))
    print(f"Chatting as {CLI_ACTOR}; conversation {conversation!r}. Type 'exit' to leave, 'reset' to clear.\n")

    async def pump(message: str) -> str:
        turn = Turn(message=message, conversation_id=conversation, actor=CLI_ACTOR)
        async for event in runtime.executive.stream_turn(turn):
            kind = event.get("type")
            if kind == "token":
                print(event.get("text", ""), end="", flush=True)
            elif kind == "tool":
                marker = "ok" if event.get("ok") else "FAILED"
                print(f"\n  [tool {event.get('tool')}: {marker} {_bounded(event.get('error', ''), 120)}]")
            elif kind == "stream_unavailable":
                print(f"\n  [streaming unavailable: {_bounded(event.get('error', ''), 120)}]")
            elif kind == "error":
                print(f"\n[error] {event.get('error')}")
            elif kind == "turn_finished":
                return event.get("result", {}).get("reply", "")
        return ""

    while True:
        try:
            message = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not message:
            continue
        if message.lower() in {"exit", "quit"}:
            return 0
        if message.lower() == "reset":
            runtime.executive.conversations.pop(conversation, None)
            print("(conversation cleared)")
            continue
        print("\nnatasha> ", end="")
        run_coroutine_sync(pump(message))
        print()


def cmd_ask(args: argparse.Namespace) -> int:
    """One-shot question."""
    import asyncio

    from ..executive import Turn

    runtime = _runtime()
    result = asyncio.run(runtime.executive.run_turn(Turn(message=args.message, actor=CLI_ACTOR,
                                                        conversation_id=args.conversation or "")))
    _print(result.to_dict() if args.json else result.reply)
    if result.error and not args.json:
        print(f"[error] {result.error}", file=sys.stderr)
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _print(_runtime().status())
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    runtime = _runtime()
    report = runtime.health()
    findings: list[str] = []
    if runtime.state.degraded:
        findings += [f"DEGRADED  {item}" for item in runtime.state.degraded]
    for name, check in (report.get("observability", {}).get("checks", {}) or {}).items():
        findings.append(f"{check['status'].upper():9s} {name}: {check['detail']}")
    capabilities = {
        "voice": runtime.voice.capabilities() if runtime.voice else {"available": False},
        "computer": runtime.computer.capabilities() if runtime.computer else {"available": False},
        "browser": runtime.browser.capabilities() if runtime.browser else {"available": False},
        "creation": runtime.creation.capabilities() if runtime.creation else {"available": False},
    }
    if args.json:
        _print({"health": report, "capabilities": capabilities, "findings": findings})
        return 0
    print(BANNER.format(version=_version(), codename=_codename()))
    print(f"home: {runtime.paths.home}")
    for line in findings:
        print(" ", line)
    print("\ncapabilities:")
    for name, value in capabilities.items():
        available = value.get("available", value.get("backends"))
        print(f"  {name:9s} available={available}  {_bounded(str(value.get('reason', '')), 90)}")
    print(f"\ntools: {len(runtime.tools.names())}  provider(s): "
          f"{len(getattr(runtime.brain.providers, 'health_snapshot', dict)()) if runtime.brain else 0}")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    from ..api.auth import get_auth_manager

    manager = get_auth_manager(session_ttl_minutes=int(_runtime().settings.session_ttl_minutes))
    if args.action == "status":
        _print({"initialised": manager.initialised(), "sessions": manager.sessions()})
        return 0
    if args.action == "setup":
        import getpass

        passphrase = args.passphrase or getpass.getpass("owner passphrase: ")
        if not args.passphrase:
            confirm = getpass.getpass("repeat passphrase: ")
            if confirm != passphrase:
                print("passphrases do not match", file=sys.stderr)
                return 2
        token = manager.setup(passphrase, owner_id=args.owner or "owner")
        _print({"ok": True, "note": "owner created", "token_prefix": token[:8]})
        return 0
    if args.action == "passphrase":
        import getpass

        current = getpass.getpass("current passphrase: ")
        new = getpass.getpass("new passphrase: ")
        try:
            manager.change_passphrase(current, new)
        except Exception as exc:
            print(f"failed: {exc}", file=sys.stderr)
            return 1
        _print({"ok": True})
        return 0
    if args.action == "revoke":
        _print({"revoked": manager.revoke_all()})
        return 0
    print(f"unknown auth action {args.action!r}", file=sys.stderr)
    return 2


def cmd_memory(args: argparse.Namespace) -> int:
    runtime = _runtime()
    store = runtime.memory
    if args.action == "list":
        records = store.list(kind=args.kind or "", limit=args.limit)
        if args.json:
            _print([record.to_dict() for record in records])
        for record in records:
            print(f"{record.id}  {record.kind.value:12s} {_bounded(record.summary, 90)}")
        return 0
    if args.action == "search":
        scored = store.recall(args.query, limit=args.limit, actor=CLI_ACTOR)
        if args.json:
            _print([item.to_dict() for item in scored])
        for item in scored:
            print(f"{item.score:.3f}  {item.memory.kind.value:12s} {_bounded(item.memory.summary, 90)}")
        return 0
    if args.action == "add":
        record = store.add(args.kind, args.content, tags=args.tag or [], actor=CLI_ACTOR,
                           importance=args.importance)
        _print(record.to_dict())
        return 0
    if args.action == "forget":
        _print(store.forget(args.id or None, query=args.query, hard=args.hard, actor=CLI_ACTOR))
        return 0
    if args.action == "stats":
        _print(store.stats())
        return 0
    print(f"unknown memory action {args.action!r}", file=sys.stderr)
    return 2


def cmd_missions(args: argparse.Namespace) -> int:
    import asyncio

    runtime = _runtime()
    engine = runtime.missions
    if args.action == "list":
        for mission in engine.store.list(limit=args.limit):
            print(f"{mission.id}  {mission.state.value:16s} {mission.progress():5.0%}  "
                  f"{_bounded(mission.title or mission.objective, 70)}")
        return 0
    if args.action == "show":
        _print(engine.get(args.id).to_dict() if args.json else engine.get(args.id).to_dict(include_steps=False))
        return 0
    if args.action == "create":
        mission = engine.create(args.objective, success_criteria=args.criterion or [],
                                verification_plan=args.verify or [], created_by=CLI_ACTOR)
        _print({"mission": mission.id, "state": mission.state.value})
        return 0
    if args.action == "run":
        result = asyncio.run(engine.run(args.id, actor=CLI_ACTOR))
        _print(result.to_dict())
        return 0 if result.ok else 1
    if args.action == "verify":
        mission = engine.get(args.id)
        report = asyncio.run(engine.verify(mission, actor=CLI_ACTOR))
        _print(report.to_dict())
        return 0 if report.passed else 1
    if args.action == "rollback":
        _print(asyncio.run(engine.rollback(args.id, actor=CLI_ACTOR, reason=args.reason or "")))
        return 0
    if args.action == "cancel":
        mission = engine.cancel(args.id, reason=args.reason or "", actor=CLI_ACTOR)
        _print(mission.to_dict(include_steps=False))
        return 0
    print(f"unknown missions action {args.action!r}", file=sys.stderr)
    return 2


def cmd_tools(args: argparse.Namespace) -> int:
    import asyncio

    runtime = _runtime()
    registry = runtime.tools
    if args.action == "list":
        for spec in registry.describe():
            flag = "approval" if spec["requires_approval"] else "auto"
            print(f"{spec['name']:24s} {spec['capability']:20s} {spec['risk']:9s} {flag:9s} "
                  f"{_bounded(spec['description'], 60)}")
        return 0
    if args.action == "run":
        arguments = json.loads(args.arguments) if args.arguments else {}
        from ..tools.base import ToolContext

        context = ToolContext(actor=CLI_ACTOR, policy=runtime.policy, memory=runtime.memory,
                              brain=runtime.brain, world=runtime.world, broker=runtime.broker,
                              approvals=runtime.approvals, workspace=runtime.paths.workspace,
                              extra={"approval_id": args.approval or ""})
        result = asyncio.run(registry.execute(args.name, arguments, context=context,
                                             approval_id=args.approval or ""))
        _print(result.to_dict())
        return 0 if result.ok else 1
    print(f"unknown tools action {args.action!r}", file=sys.stderr)
    return 2


def cmd_approvals(args: argparse.Namespace) -> int:
    runtime = _runtime()
    engine = runtime.approvals
    if args.action == "list":
        for request in engine.pending(limit=args.limit):
            print(f"{request.id}  {request.risk.name:9s} {request.operation:24s} "
                  f"{_bounded(request.resource, 50)}  expires {request.expires_at}")
        return 0
    if args.action == "history":
        _print(engine.history(limit=args.limit))
        return 0
    if args.action == "approve":
        approval = engine.approve(args.id, decided_by=CLI_ACTOR, note=args.note or "",
                                  ttl_seconds=args.ttl)
        _print(approval.to_dict())
        return 0
    if args.action == "deny":
        _print(engine.deny(args.id, decided_by=CLI_ACTOR, note=args.note or "").to_dict())
        return 0
    if args.action == "revoke":
        _print({"revoked": engine.revoke(args.id, actor=CLI_ACTOR)})
        return 0
    print(f"unknown approvals action {args.action!r}", file=sys.stderr)
    return 2


def cmd_activity(args: argparse.Namespace) -> int:
    runtime = _runtime()
    log = runtime.log
    if args.action == "tail":
        for event in log.query(limit=args.limit, descending=True):
            print(f"{event.ts}  {event.kind.value:16s} {event.actor:14s} "
                  f"{_bounded(json.dumps(event.payload, default=str), 90)}")
        return 0
    if args.action == "verify":
        ok, detail = log.verify_chain()
        print(f"hash chain: {'valid' if ok else 'BROKEN'}  {json.dumps(detail, default=str)}")
        return 0 if ok else 1
    if args.action == "export":
        target = Path(args.path) if args.path else runtime.paths.reports / "events-export.jsonl"
        log.export_jsonl(target, verify=True)
        print(f"exported {log.count()} events to {target}")
        return 0
    if args.action == "stats":
        _print(log.stats())
        return 0
    print(f"unknown activity action {args.action!r}", file=sys.stderr)
    return 2


def cmd_credentials(args: argparse.Namespace) -> int:
    runtime = _runtime()
    manager = runtime.credentials
    if args.action == "list":
        for item in manager.list():
            print(f"{item['ref']:34s} {item['kind']:16s} {item['status']:9s} "
                  f"{_bounded(item.get('label', ''), 40)}")
        return 0
    if args.action == "add":
        secret = args.secret
        if not secret:
            import getpass

            secret = getpass.getpass("secret (not echoed): ")
        reference = args.name if args.name.startswith("credential://") else f"credential://{args.name}"
        metadata = manager.store(reference, secret, kind=args.kind, provider=args.name,
                                 label=args.label or args.name, actor=CLI_ACTOR)
        _print(metadata.to_dict())
        return 0
    if args.action == "rotate":
        import getpass

        secret = args.secret or getpass.getpass("new secret: ")
        reference = args.name if args.name.startswith("credential://") else f"credential://{args.name}"
        _print(manager.rotate(reference, secret, actor=CLI_ACTOR).to_dict())
        return 0
    if args.action == "revoke":
        reference = args.name if args.name.startswith("credential://") else f"credential://{args.name}"
        manager.revoke(reference, actor=CLI_ACTOR)
        print(f"revoked {reference}")
        return 0
    if args.action == "test":
        reference = args.name if args.name.startswith("credential://") else f"credential://{args.name}"
        _print(manager.test_connection(reference, actor=CLI_ACTOR))
        return 0
    if args.action == "stats":
        _print(manager.stats())
        return 0
    print(f"unknown credentials action {args.action!r}", file=sys.stderr)
    return 2


def cmd_skills(args: argparse.Namespace) -> int:
    import asyncio

    runtime = _runtime()
    lifecycle = runtime.skills
    if args.action == "list":
        for record in lifecycle.list():
            print(f"{record.skill_id:24s} {record.version:9s} {record.state.value:10s} "
                  f"{_bounded(record.manifest.description if record.manifest else '', 50)}")
        return 0
    if args.action == "install":
        record = lifecycle.install(args.path, activate=not args.no_activate, approved_by=CLI_ACTOR)
        _print(record.to_dict())
        return 0
    if args.action == "test":
        _print(asyncio.run(lifecycle.test_async(args.id)))
        return 0
    if args.action == "run":
        payload = json.loads(args.payload) if args.payload else {}
        result = asyncio.run(runtime.skill_runtime.run(args.id, payload, actor=CLI_ACTOR))
        _print(result.to_dict())
        return 0 if result.ok else 1
    if args.action == "disable":
        _print(lifecycle.disable(args.id).to_dict())
        return 0
    if args.action == "enable":
        _print(lifecycle.activate(args.id).to_dict())
        return 0
    if args.action == "uninstall":
        _print(lifecycle.uninstall(args.id, purge=args.purge).to_dict())
        return 0
    print(f"unknown skills action {args.action!r}", file=sys.stderr)
    return 2


def cmd_marketplace(args: argparse.Namespace) -> int:
    runtime = _runtime()
    installer = runtime.marketplace
    if args.action == "installed":
        for item in installer.registry.list():
            print(f"{item.kind:8s} {item.name:24s} {item.version:9s} pinned={item.pinned} "
                  f"checksum={item.checksum[:12]}")
        return 0
    if args.action == "review":
        _print(installer.review(args.ref, kind=args.kind).to_dict())
        return 0
    if args.action == "install":
        _print(installer.install(args.ref, kind=args.kind, actor=CLI_ACTOR,
                                 approve_permissions=args.agree, approval_id=args.approval or "",
                                 activate=not args.no_activate))
        return 0
    if args.action == "uninstall":
        _print(installer.uninstall(args.kind, args.name, actor=CLI_ACTOR, purge=args.purge))
        return 0
    if args.action == "rollback":
        _print(installer.rollback(args.kind, args.name, to_version=args.version, actor=CLI_ACTOR))
        return 0
    print(f"unknown marketplace action {args.action!r}", file=sys.stderr)
    return 2


def cmd_mcp(args: argparse.Namespace) -> int:
    import asyncio

    runtime = _runtime()
    registry = runtime.mcp
    if args.action == "list":
        for server in registry.all():
            print(f"{server.name:20s} {server.transport:6s} {server.state.value:12s} "
                  f"enabled={server.enabled} tools={len(server.tools)}")
        return 0
    if args.action == "add":
        from ..mcp.registry import MCPServer

        command = args.command or []
        server = MCPServer(name=args.name, transport=args.transport,
                           command=command[0] if command else "", args=command[1:], url=args.url or "")
        _print(registry.configure(server, actor=CLI_ACTOR).to_dict())
        return 0
    if args.action == "install":
        _print(asyncio.run(registry.install(args.name, actor=CLI_ACTOR)).to_dict())
        return 0
    if args.action == "tools":
        for tool in registry.get(args.name).tools:
            print(f"{tool.get('name'):24s} {_bounded(tool.get('description', ''), 70)}")
        return 0
    if args.action == "call":
        arguments = json.loads(args.arguments) if args.arguments else {}
        _print(asyncio.run(registry.call(args.name, args.tool, arguments)))
        return 0
    if args.action == "disable":
        _print(asyncio.run(registry.disable(args.name, actor=CLI_ACTOR)).to_dict())
        return 0
    if args.action == "enable":
        _print(asyncio.run(registry.enable(args.name, actor=CLI_ACTOR)).to_dict())
        return 0
    if args.action == "uninstall":
        asyncio.run(registry.uninstall(args.name, actor=CLI_ACTOR))
        print(f"uninstalled {args.name}")
        return 0
    if args.action == "health":
        _print(asyncio.run(registry.health(args.name)))
        return 0
    print(f"unknown mcp action {args.action!r}", file=sys.stderr)
    return 2


def cmd_governance(args: argparse.Namespace) -> int:
    from ..governance import get_constitution

    runtime = _runtime()
    constitution = get_constitution(settings=runtime.settings, log=runtime.log)
    if args.action == "verify":
        report = constitution.verify(vault=runtime.credentials, approvals=runtime.approvals)
        _print(report.to_dict())
        for invariant in constitution.invariants(vault=runtime.credentials, approvals=runtime.approvals):
            ok, detail = invariant.check()
            print(f"{'PASS' if ok else 'FAIL'}  {invariant.id:28s} {_bounded(detail, 80)}")
        return 0 if report.ok else 1
    if args.action == "manifest":
        ok, mismatches = constitution.verify_manifest()
        print("manifest:", "matches" if ok else "MISMATCH")
        for item in mismatches:
            print("  ", item)
        return 0 if ok else 1
    if args.action == "refresh-manifest":
        path = constitution.write_manifest()
        print(f"manifest written to {path}")
        return 0
    if args.action == "protect":
        from ..governance.constitution import PROTECTED_AREAS, PROTECTED_PATHS

        print("protected areas (never self-modifiable):")
        for name, description in PROTECTED_AREAS.items():
            print(f"  {name:22s} {description}")
        print("\nprotected paths:")
        for path in PROTECTED_PATHS:
            print("  ", path)
        return 0
    print(f"unknown governance action {args.action!r}", file=sys.stderr)
    return 2


def cmd_upgrades(args: argparse.Namespace) -> int:
    from ..governance import get_constitution, get_upgrade_governor

    runtime = _runtime()
    governor = get_upgrade_governor(constitution=get_constitution(settings=runtime.settings, log=runtime.log),
                                    approvals=runtime.approvals)
    if args.action == "list":
        _print(governor.list(status=args.status or "", limit=args.limit))
        return 0
    if args.action == "opportunities":
        _print(governor.detect_opportunities(limit=args.limit))
        return 0
    if args.action == "propose":
        patch = Path(args.patch).read_text(encoding="utf-8") if args.patch and Path(args.patch).is_file() else (args.patch or "")
        proposal = governor.propose(args.title, rationale=args.rationale or args.title, patch=patch,
                                    target_files=args.target or [], actor="local-owner")
        _print(proposal.to_dict())
        return 0
    if args.action == "test":
        proposal = governor.get(args.id)
        _print(governor.sandbox_test(proposal, timeout=args.timeout).to_dict())
        return 0
    if args.action == "analyse":
        proposal = governor.get(args.id)
        governor.analyse(proposal)
        governor.risk_report(proposal)
        _print(proposal.to_dict())
        return 0
    if args.action == "request-approval":
        _print(governor.request_approval(args.id, ttl_seconds=args.ttl).to_dict())
        return 0
    if args.action == "apply":
        proposal = governor.apply(args.id, actor=CLI_ACTOR, approval_id=args.approval or "",
                                  allow_protected=args.allow_protected)
        _print(proposal.to_dict())
        return 0
    if args.action == "rollback":
        _print(governor.rollback(args.id, actor=CLI_ACTOR).to_dict())
        return 0
    print(f"unknown upgrades action {args.action!r}", file=sys.stderr)
    return 2


def cmd_create(args: argparse.Namespace) -> int:
    import asyncio

    runtime = _runtime()
    options: dict[str, Any] = {}
    if args.options:
        options = json.loads(args.options)
    job = asyncio.run(runtime.creation.create(args.kind, args.brief, **options))
    _print(job.to_dict() if args.json else
           {"status": job.status, "artifacts": [item["path"] for item in job.artifacts],
            "notes": job.notes, "error": job.error,
            "stages": [f"{item['stage']}: {item['state']}" for item in job.stages]})
    return 0 if job.status in ("succeeded", "partial") else 1


def cmd_voice(args: argparse.Namespace) -> int:
    runtime = _runtime()
    if args.action == "capabilities":
        _print(runtime.voice.capabilities())
        return 0
    if args.action == "voices":
        _print(runtime.voice.list_voices())
        return 0
    if args.action == "speak":
        result = runtime.voice.speak(args.text, voice=args.voice or "", out_path=args.out or "",
                                     play=args.play, actor=CLI_ACTOR)
        _print(result.to_dict())
        return 0 if result.ok else 1
    if args.action == "transcribe":
        _print(runtime.voice.transcribe(args.path, actor=CLI_ACTOR))
        return 0
    print(f"unknown voice action {args.action!r}", file=sys.stderr)
    return 2


def cmd_agents(args: argparse.Namespace) -> int:
    import asyncio

    runtime = _runtime()
    if args.action == "roles":
        for role in runtime.agents.roles():
            print(f"{role['name']:12s} {role['max_risk']:7s} "
                  f"tools={len(role['tools'])}  {_bounded(role['description'], 60)}")
        return 0
    if args.action == "workers":
        _print(runtime.supervisor.stats())
        return 0
    if args.action == "delegate":
        context = json.loads(args.context) if args.context else {}
        result = asyncio.run(runtime.agents.delegate(args.role, args.objective, context=context,
                                                     actor=CLI_ACTOR))
        _print(result)
        return 0 if result.get("ok") else 1
    print(f"unknown agents action {args.action!r}", file=sys.stderr)
    return 2


def cmd_providers(args: argparse.Namespace) -> int:
    runtime = _runtime()
    brain = runtime.brain
    if args.action in ("list", "health"):
        snapshot = brain.providers.health_snapshot()
        if not snapshot:
            print("no providers configured. Add one in Settings, or start a local server "
                  "(Ollama: http://127.0.0.1:11434, llama.cpp: http://127.0.0.1:8080).")
            return 0
        for name, status in sorted(snapshot.items()):
            print(f"{name:20s} {status.get('state', '?'):10s} {_bounded(str(status.get('detail', '')), 60)}")
        return 0
    if args.action == "models":
        models = brain.providers.models.list() if hasattr(brain.providers.models, "list") else []
        for model in models:
            flags = ",".join(sorted(capability.value for capability in getattr(model, "capabilities", set())))
            print(f"{model.provider:16s} {model.id:32s} ctx={model.context_window:>7d} {flags}")
        return 0
    if args.action == "test":
        import asyncio

        from ..brain import ChatMessage, CompletionRequest

        try:
            response = asyncio.run(brain.complete(CompletionRequest(
                messages=[ChatMessage.user("Reply with the single word: ready")], task="chat",
                provider=args.provider or "", model=args.model or "")))
            print(f"{response.provider}/{response.model}: {_bounded(response.content, 80)}")
            return 0
        except Exception as exc:
            print(f"provider test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    print(f"unknown providers action {args.action!r}", file=sys.stderr)
    return 2


def cmd_backup(args: argparse.Namespace) -> int:
    import shutil
    import tarfile

    runtime = _runtime()
    target = Path(args.path) if args.path else runtime.paths.reports / "natasha-backup.tar.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, "w:gz") as archive:
        for name in ("config", "db", "skills", "marketplace", "artifacts", "missions", "mcp"):
            directory = runtime.paths.home / name
            if directory.exists():
                archive.add(directory, arcname=name)
    size = target.stat().st_size
    print(f"backup written to {target} ({size / 1024:.1f} KiB)")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from ..db.migrations import MigrationRunner

    runner = MigrationRunner()
    if args.action == "status":
        _print(runner.status())
        return 0
    if args.action == "up":
        _print(runner.upgrade(target=args.target or "head"))
        return 0
    if args.action == "down":
        _print(runner.downgrade(steps=args.steps))
        return 0
    if args.action == "create":
        _print({"path": str(runner.create(args.name))})
        return 0
    print(f"unknown migrate action {args.action!r}", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="natasha", description="Natasha - local-first personal agent")
    parser.add_argument("--version", action="version", version=f"natasha {_version()} ({_codename()})")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start the API and UI server")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=cmd_serve)

    chat = sub.add_parser("chat", help="interactive chat")
    chat.add_argument("--conversation", default="")
    chat.set_defaults(func=cmd_chat)

    ask = sub.add_parser("ask", help="ask one question and exit")
    ask.add_argument("message")
    ask.add_argument("--conversation", default="")
    ask.set_defaults(func=cmd_ask)

    status = sub.add_parser("status", help="show runtime status")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="explain what works and what does not")
    doctor.set_defaults(func=cmd_doctor)

    auth = sub.add_parser("auth", help="owner authentication")
    auth.add_argument("action", choices=["status", "setup", "passphrase", "revoke"])
    auth.add_argument("--passphrase")
    auth.add_argument("--owner")
    auth.set_defaults(func=cmd_auth)

    memory = sub.add_parser("memory", help="memory operations")
    memory.add_argument("action", choices=["list", "search", "add", "forget", "stats"])
    memory.add_argument("query", nargs="?", default="")
    memory.add_argument("--kind", default="")
    memory.add_argument("--content", default="")
    memory.add_argument("--tag", action="append")
    memory.add_argument("--importance", type=float, default=0.5)
    memory.add_argument("--id", default="")
    memory.add_argument("--hard", action="store_true")
    memory.add_argument("--limit", type=int, default=20)
    memory.set_defaults(func=cmd_memory)

    missions = sub.add_parser("missions", help="mission control")
    missions.add_argument("action", choices=["list", "show", "create", "run", "verify", "rollback", "cancel"])
    missions.add_argument("id", nargs="?", default="")
    missions.add_argument("objective", nargs="?", default="")
    missions.add_argument("--criterion", action="append")
    missions.add_argument("--verify", action="append")
    missions.add_argument("--reason", default="")
    missions.add_argument("--limit", type=int, default=20)
    missions.set_defaults(func=cmd_missions)

    tools = sub.add_parser("tools", help="tool inventory and execution")
    tools.add_argument("action", choices=["list", "run"])
    tools.add_argument("name", nargs="?", default="")
    tools.add_argument("--arguments", default="")
    tools.add_argument("--approval", default="")
    tools.set_defaults(func=cmd_tools)

    approvals = sub.add_parser("approvals", help="owner approvals")
    approvals.add_argument("action", choices=["list", "history", "approve", "deny", "revoke"])
    approvals.add_argument("id", nargs="?", default="")
    approvals.add_argument("--note", default="")
    approvals.add_argument("--ttl", type=int, default=900)
    approvals.add_argument("--limit", type=int, default=50)
    approvals.set_defaults(func=cmd_approvals)

    activity = sub.add_parser("activity", help="audit log")
    activity.add_argument("action", choices=["tail", "verify", "export", "stats"])
    activity.add_argument("--limit", type=int, default=30)
    activity.add_argument("--path")
    activity.set_defaults(func=cmd_activity)

    credentials = sub.add_parser("credentials", help="credential vault")
    credentials.add_argument("action", choices=["list", "add", "rotate", "revoke", "test", "stats"])
    credentials.add_argument("name", nargs="?", default="")
    credentials.add_argument("--secret")
    credentials.add_argument("--kind", default="api_key")
    credentials.add_argument("--label", default="")
    credentials.set_defaults(func=cmd_credentials)

    skills = sub.add_parser("skills", help="skill lifecycle")
    skills.add_argument("action", choices=["list", "install", "test", "run", "enable", "disable", "uninstall"])
    skills.add_argument("id", nargs="?", default="")
    skills.add_argument("path", nargs="?", default="")
    skills.add_argument("--payload")
    skills.add_argument("--no-activate", action="store_true")
    skills.add_argument("--purge", action="store_true")
    skills.set_defaults(func=cmd_skills)

    marketplace = sub.add_parser("marketplace", help="skill/plugin marketplace")
    marketplace.add_argument("action", choices=["installed", "review", "install", "uninstall", "rollback"])
    marketplace.add_argument("ref", nargs="?", default="")
    marketplace.add_argument("--name", default="")
    marketplace.add_argument("--kind", default="skill")
    marketplace.add_argument("--version", default="")
    marketplace.add_argument("--agree", action="store_true", help="accept the package's permission requests")
    marketplace.add_argument("--approval", default="")
    marketplace.add_argument("--no-activate", action="store_true")
    marketplace.add_argument("--purge", action="store_true")
    marketplace.set_defaults(func=cmd_marketplace)

    mcp = sub.add_parser("mcp", help="MCP servers")
    mcp.add_argument("action", choices=["list", "add", "install", "tools", "call", "enable", "disable",
                                        "uninstall", "health"])
    mcp.add_argument("name", nargs="?", default="")
    mcp.add_argument("tool", nargs="?", default="")
    mcp.add_argument("--command", nargs="*")
    mcp.add_argument("--url")
    mcp.add_argument("--transport", default="stdio")
    mcp.add_argument("--arguments")
    mcp.set_defaults(func=cmd_mcp)

    governance = sub.add_parser("governance", help="constitution checks")
    governance.add_argument("action", choices=["verify", "manifest", "refresh-manifest", "protect"])
    governance.set_defaults(func=cmd_governance)

    upgrades = sub.add_parser("upgrades", help="controlled self-improvement")
    upgrades.add_argument("action", choices=["list", "opportunities", "propose", "analyse", "test",
                                             "request-approval", "apply", "rollback"])
    upgrades.add_argument("id", nargs="?", default="")
    upgrades.add_argument("--title", default="")
    upgrades.add_argument("--rationale", default="")
    upgrades.add_argument("--patch", default="")
    upgrades.add_argument("--target", action="append")
    upgrades.add_argument("--approval", default="")
    upgrades.add_argument("--allow-protected", action="store_true")
    upgrades.add_argument("--status", default="")
    upgrades.add_argument("--limit", type=int, default=50)
    upgrades.add_argument("--ttl", type=int, default=3600)
    upgrades.add_argument("--timeout", type=int, default=600)
    upgrades.set_defaults(func=cmd_upgrades)

    create = sub.add_parser("create", help="create a document, image, audio, video or slides")
    create.add_argument("kind", choices=["document", "image", "audio", "video", "slides"])
    create.add_argument("brief")
    create.add_argument("--options", default="")
    create.set_defaults(func=cmd_create)

    voice = sub.add_parser("voice", help="speech in/out")
    voice.add_argument("action", choices=["capabilities", "voices", "speak", "transcribe"])
    voice.add_argument("text", nargs="?", default="")
    voice.add_argument("--path", default="")
    voice.add_argument("--voice", default="")
    voice.add_argument("--out", default="")
    voice.add_argument("--play", action="store_true")
    voice.set_defaults(func=cmd_voice)

    agents = sub.add_parser("agents", help="worker fleet")
    agents.add_argument("action", choices=["roles", "workers", "delegate"])
    agents.add_argument("role", nargs="?", default="")
    agents.add_argument("objective", nargs="?", default="")
    agents.add_argument("--context")
    agents.set_defaults(func=cmd_agents)

    providers = sub.add_parser("providers", help="model providers")
    providers.add_argument("action", choices=["list", "health", "models", "test"])
    providers.add_argument("--provider", default="")
    providers.add_argument("--model", default="")
    providers.set_defaults(func=cmd_providers)

    backup = sub.add_parser("backup", help="back up Natasha's state")
    backup.add_argument("--path")
    backup.set_defaults(func=cmd_backup)

    migrate = sub.add_parser("migrate", help="database migrations")
    migrate.add_argument("action", choices=["status", "up", "down", "create"])
    migrate.add_argument("--target", default="")
    migrate.add_argument("--steps", type=int, default=1)
    migrate.add_argument("--name", default="")
    migrate.set_defaults(func=cmd_migrate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        if os.environ.get("NATASHA_DEBUG"):
            raise
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
