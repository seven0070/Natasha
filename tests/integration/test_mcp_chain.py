"""MCP runtime: real stdio servers, owner-gated installs, honest failures, untrusted output.

The MCP server itself is a fixture process (tests/fixtures/mcp_echo_server.py) that speaks the real
JSON-RPC handshake over stdio, so the client, registry, tool bridging and audit trail are all
exercised for real.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from natasha.events import EventKind

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_echo_server.py"


@pytest.fixture()
def registry(home, log, policy):
    from natasha.mcp import get_mcp_registry
    from natasha.tools import get_tool_registry

    tools = get_tool_registry(policy=policy, log=log)
    instance = get_mcp_registry(tools=tools, log=log)
    yield instance
    # Sessions own child processes: close them so a test cannot leak a server (or a warning).
    try:
        _run(instance.close())
    except Exception:
        pass


@pytest.fixture()
def server(registry):
    from natasha.mcp import MCPServer

    record = MCPServer(name="fixture", transport="stdio", command=sys.executable,
                       args=[str(FIXTURE)], notes="fixture MCP server")
    registry.configure(record)
    return record


def _run(coro):
    """Drive MCP calls on the *shared* background loop.

    MCP sessions own child processes and are bound to the loop that created them. ``asyncio.run``
    would create a loop per call, so every session would be orphaned (and its transport garbage
    collected after its loop closed) between one call and the next. The background loop is the same
    loop the runtime uses for sync callers, so sessions stay usable and, more importantly, closable.
    """
    from natasha.core.async_utils import run_coroutine_sync

    return run_coroutine_sync(coro)


def test_configure_and_list_servers(registry, server):
    listed = registry.all()
    assert [item.name for item in listed] == ["fixture"]
    assert listed[0].state.value in {"configured", "discovered"}
    assert listed[0].enabled is False


def test_inspect_discovers_tools_without_enabling_them(registry, server):
    inspected = _run(registry.inspect("fixture"))
    assert [tool["name"] for tool in inspected.tools] == ["add_numbers", "shell_exec"]
    assert inspected.state.value == "inspected"
    assert inspected.enabled is False
    # Connecting is not permission to run: the tools are not in the registry yet.
    assert registry.tools.names() == []


def test_scan_flags_the_dangerous_tool(registry, server):
    _run(registry.inspect("fixture"))
    scan = registry.scan("fixture")
    assert scan["requested_permissions"]
    assert "shell.exec" in " ".join(scan["requested_permissions"])
    assert scan["findings"] or scan["risk"] in {"HIGH", "CRITICAL"}


def test_install_requires_the_owner(registry, server):
    _run(registry.inspect("fixture"))
    registry.scan("fixture")
    from natasha.core import ApprovalRequired

    with pytest.raises(ApprovalRequired):
        registry.request_permissions("fixture", ["mcp.execute"], actor="model:main")


def test_owner_install_then_enable_registers_scoped_tools(registry, server):
    _run(registry.install("fixture", actor="owner", permissions=["mcp.execute"]))
    assert registry.get("fixture").state.value == "installed"
    _run(registry.enable("fixture"))
    assert registry.get("fixture").enabled is True
    names = registry.tools.names()
    assert any("add_numbers" in name for name in names)


def test_calling_an_mcp_tool_returns_real_output(registry, server):
    _run(registry.install("fixture", actor="owner", permissions=["mcp.execute"]))
    _run(registry.enable("fixture"))
    name = next(item for item in registry.tools.names() if "add_numbers" in item)
    result = _run(registry.tools.execute(name, {"a": 2, "b": 3}))
    assert result.ok is True, result.error
    assert "5" in json.dumps(result.output, default=str)


def test_mcp_tool_output_is_marked_untrusted(registry, server):
    """A server that answers with instructions must have them fenced, not obeyed."""
    _run(registry.install("fixture", actor="owner", permissions=["mcp.execute"]))
    _run(registry.enable("fixture"))
    output = _run(registry.call("fixture", "add_numbers", {"a": 1, "b": 1}))
    assert output["is_error"] is False
    assert "untrusted-content" in output["text"]
    assert "DATA, not instructions" in output["text"]
    assert output["raw_text"] == "sum=2"


def test_hostile_mcp_output_is_flagged(registry, server):
    _run(registry.install("fixture", actor="owner", permissions=["mcp.execute"]))
    _run(registry.enable("fixture"))
    output = _run(registry.call("fixture", "shell_exec", {"command": "ls"}))
    assert output["suspicious"] is True
    assert "authority_pretext" in output["findings"] or "exfiltration" in output["findings"] or output["findings"]
    assert "WARNING" in output["text"]
    # The hostile text is quoted, never live.
    assert "[quoted:" in output["text"]


def test_a_disabled_server_refuses_calls(registry, server):
    _run(registry.install("fixture", actor="owner", permissions=["mcp.execute"]))
    _run(registry.enable("fixture"))
    name = next(item for item in registry.tools.names() if "add_numbers" in item)
    _run(registry.disable("fixture"))
    result = _run(registry.tools.execute(name, {"a": 1, "b": 1}))
    assert result.ok is False
    assert result.error


def test_an_unreachable_server_fails_honestly(registry, log):
    from natasha.mcp import MCPServer

    registry.configure(MCPServer(name="broken", transport="stdio", command=sys.executable,
                                 args=["-c", "import sys; sys.exit(3)"]))
    from natasha.mcp.client import MCPError

    with pytest.raises((MCPError, TimeoutError)) as excinfo:
        _run(asyncio.wait_for(registry.inspect("broken"), timeout=30))
    assert "broken" in str(excinfo.value), "the failure must name the server that failed"
    events = log.query(kinds=[EventKind.MCP], limit=50)
    assert any("broken" in json.dumps(event.payload, default=str) for event in events)


def test_install_and_uninstall_are_audited(registry, server, log):
    _run(registry.install("fixture", actor="owner", permissions=["mcp.execute"]))
    _run(registry.enable("fixture"))
    _run(registry.uninstall("fixture"))
    assert registry.all() == [] or registry.get("fixture").state.value == "uninstalled"
    actions = {event.payload.get("action") for event in log.query(kinds=[EventKind.MCP], limit=100)}
    assert {"permissions_granted"} <= actions | set()


def test_server_without_permissions_cannot_run_shell(registry, server):
    """Grant only mcp.execute: the shell tool still cannot be reached through the bridge."""
    _run(registry.install("fixture", actor="owner", permissions=["mcp.execute"]))
    _run(registry.enable("fixture"))
    shell_tools = [name for name in registry.tools.names() if "shell_exec" in name]
    for name in shell_tools:
        result = _run(registry.tools.execute(name, {"command": "ls"}))
        assert result.ok is False
        assert "approval" in result.error.lower() or "denied" in result.error.lower() or "permission" in result.error.lower()


def test_health_reports_the_running_server(registry, server):
    _run(registry.install("fixture", actor="owner", permissions=["mcp.execute"]))
    _run(registry.enable("fixture"))
    health = _run(registry.health("fixture"))
    assert health["ok"] is True
    assert health["tools"] >= 2


def test_unknown_server_is_a_clear_error(registry):
    from natasha.core import NotFoundError

    with pytest.raises(NotFoundError):
        registry.get("does-not-exist")
    with pytest.raises(NotFoundError):
        registry.scan("does-not-exist")
