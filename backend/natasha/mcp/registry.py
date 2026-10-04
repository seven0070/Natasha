"""MCP server lifecycle: discovered -> inspected -> permissions approved -> installed -> enabled.

Nothing here happens automatically. A server is installed only after the owner sees its tool list and
grants the requested permissions; tools are namespaced; every call is policy-checked and audited.
"""

from __future__ import annotations

import enum
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any

from ..core import ApprovalRequired, ConflictError, MCPError, NotFoundError, get_paths
from ..core.clock import iso
from ..core.risk import RiskLevel
from ..events import EventKind, EventLog, get_event_log
from ..security.policy import Capability
from .client import DEFAULT_TIMEOUT, MCPClient, MCPTool, StdioTransport, StreamableHTTPTransport

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    name TEXT PRIMARY KEY,
    transport TEXT NOT NULL,
    command TEXT DEFAULT '',
    args TEXT DEFAULT '[]',
    url TEXT DEFAULT '',
    env TEXT DEFAULT '{}',
    state TEXT NOT NULL,
    trust TEXT DEFAULT 'untrusted',
    permissions TEXT DEFAULT '[]',
    tools TEXT DEFAULT '[]',
    server_info TEXT DEFAULT '{}',
    installed_at TEXT DEFAULT '',
    last_seen TEXT DEFAULT '',
    enabled INTEGER DEFAULT 0,
    notes TEXT DEFAULT ''
);
"""

#: Tool names that imply a capability, so the owner sees the real risk before enabling.
CAPABILITY_HINTS: dict[str, Capability] = {
    "shell": Capability.SHELL_EXEC, "exec": Capability.SHELL_EXEC, "command": Capability.SHELL_EXEC,
    "run": Capability.CODE_EXEC, "python": Capability.CODE_EXEC, "eval": Capability.CODE_EXEC,
    "read_file": Capability.FS_READ, "write_file": Capability.FS_WRITE, "delete": Capability.FS_DELETE,
    "fetch": Capability.NET_HTTP, "http": Capability.NET_HTTP, "request": Capability.NET_HTTP,
    "browser": Capability.BROWSER_CONTROL, "screenshot": Capability.SCREEN_CAPTURE,
    "click": Capability.INPUT_CONTROL, "type": Capability.INPUT_CONTROL,
    "credential": Capability.CREDENTIAL_USE, "secret": Capability.CREDENTIAL_USE,
    "database": Capability.NET_SOCKET, "sql": Capability.NET_SOCKET,
}


class MCPServerState(str, enum.Enum):
    DISCOVERED = "discovered"
    INSPECTED = "inspected"
    SCANNED = "scanned"
    AWAITING_PERMISSIONS = "awaiting_permissions"
    INSTALLED = "installed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    FAILED = "failed"
    UNINSTALLED = "uninstalled"


@dataclass
class MCPServer:
    """A configured MCP server."""

    name: str
    transport: str = "stdio"            # stdio | http
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    state: MCPServerState = MCPServerState.DISCOVERED
    trust: str = "untrusted"
    permissions: list[str] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    server_info: dict[str, Any] = field(default_factory=dict)
    installed_at: str = ""
    last_seen: str = ""
    enabled: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "transport": self.transport, "command": self.command,
                "args": self.args, "url": self.url, "state": self.state.value, "trust": self.trust,
                "permissions": self.permissions, "tools": self.tools, "server_info": self.server_info,
                "installed_at": self.installed_at, "last_seen": self.last_seen, "enabled": self.enabled,
                "notes": self.notes}

    def build_client(self) -> MCPClient:
        if self.transport == "stdio":
            if not self.command:
                raise MCPError(f"MCP server {self.name!r} has no command configured")
            return MCPClient(self.name, StdioTransport(self.command, self.args, env=self.env))
        if self.transport == "http":
            if not self.url:
                raise MCPError(f"MCP server {self.name!r} has no url configured")
            return MCPClient(self.name, StreamableHTTPTransport(self.url))
        raise MCPError(f"unknown MCP transport {self.transport!r}")


class MCPServerRegistry:
    """Persistent inventory of MCP servers plus the tool bridge."""

    def __init__(self, *, db_path: str | None = None, tools: Any = None, log: EventLog | None = None,
                 approvals: Any = None) -> None:
        self.db_path = str(db_path or get_paths().db_path("mcp"))
        self.tools = tools
        self.log = log or get_event_log()
        self.approvals = approvals
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._clients: dict[str, MCPClient] = {}
        with self._conn:
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ inventory
    def configure(self, server: MCPServer, *, actor: str = "owner") -> MCPServer:
        """Register a discovered server (no tools are exposed yet)."""
        if not server.name or not server.name.replace("-", "").replace("_", "").isalnum():
            raise ConflictError("MCP server names must be alphanumeric with - or _")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO mcp_servers (name, transport, command, args, url, env, state, trust,"
                " permissions, tools, server_info) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET transport=excluded.transport, command=excluded.command,"
                " args=excluded.args, url=excluded.url, env=excluded.env",
                (server.name, server.transport, server.command, json.dumps(server.args), server.url,
                 json.dumps(server.env), server.state.value, server.trust, json.dumps(server.permissions),
                 json.dumps(server.tools), json.dumps(server.server_info)))
        self._event("configured", server.name, {"transport": server.transport}, actor)
        return self.get(server.name)

    def get(self, name: str) -> MCPServer:
        with self._lock:
            row = self._conn.execute("SELECT * FROM mcp_servers WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise NotFoundError(f"MCP server {name!r} is not configured")
        return self._from_row(row)

    def all(self) -> list[MCPServer]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM mcp_servers ORDER BY name").fetchall()
        return [self._from_row(row) for row in rows]

    def _from_row(self, row: Any) -> MCPServer:
        return MCPServer(
            name=row["name"], transport=row["transport"], command=row["command"] or "",
            args=json.loads(row["args"] or "[]"), url=row["url"] or "",
            env=json.loads(row["env"] or "{}"), state=MCPServerState(row["state"]), trust=row["trust"],
            permissions=json.loads(row["permissions"] or "[]"), tools=json.loads(row["tools"] or "[]"),
            server_info=json.loads(row["server_info"] or "{}"), installed_at=row["installed_at"] or "",
            last_seen=row["last_seen"] or "", enabled=bool(row["enabled"]), notes=row["notes"] or "",
        )

    def _save(self, server: MCPServer) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE mcp_servers SET state=?, trust=?, permissions=?, tools=?, server_info=?,"
                " installed_at=?, last_seen=?, enabled=?, notes=? WHERE name=?",
                (server.state.value, server.trust, json.dumps(server.permissions), json.dumps(server.tools),
                 json.dumps(server.server_info), server.installed_at, server.last_seen,
                 int(server.enabled), server.notes, server.name))

    # ------------------------------------------------------------------ lifecycle
    async def inspect(self, name: str, *, actor: str = "owner") -> MCPServer:
        """Connect, read the server's identity and list its tools (nothing is enabled)."""
        server = self.get(name)
        client = server.build_client()
        try:
            info = await client.connect()
            tools = await client.list_tools()
        except Exception as exc:
            server.state = MCPServerState.FAILED
            # Name the configured server, not just its command: "python exited with 3" does not tell
            # an operator which of their MCP servers is broken.
            server.notes = f"MCP server {name!r} failed inspection: {type(exc).__name__}: {exc}"
            self._save(server)
            self._event("inspect_failed", name, {"error": server.notes}, actor, RiskLevel.MEDIUM)
            raise MCPError(server.notes) from exc
        await client.close()
        server.server_info = info.get("server_info", {})
        server.tools = [tool.to_dict() for tool in tools]
        server.state = MCPServerState.INSPECTED
        server.last_seen = iso()
        self._save(server)
        self._event("inspected", name, {"tools": len(server.tools),
                                        "free": [tool.name for tool in tools if not client.tool_by_name(tool.name).annotations.get("destructive")]},
                    actor)
        return server

    def scan(self, name: str, *, actor: str = "owner") -> dict[str, Any]:
        """Static review: what could this server do, and what is it asking for?"""
        server = self.get(name)
        findings: list[dict[str, Any]] = []
        required: set[str] = set()
        for tool in server.tools:
            name_lower = tool["name"].lower()
            for hint, capability in CAPABILITY_HINTS.items():
                if hint in name_lower:
                    required.add(capability.value)
                    findings.append({"tool": tool["name"], "capability": capability.value,
                                     "risk": RiskLevel.HIGH.name if capability in (
                                         Capability.SHELL_EXEC, Capability.CODE_EXEC,
                                         Capability.CREDENTIAL_USE, Capability.FS_DELETE) else RiskLevel.MEDIUM.name,
                                     "reason": f"tool name implies {capability.value}"})
            if tool.get("annotations", {}).get("destructive"):
                required.add(Capability.FS_DELETE.value)
                findings.append({"tool": tool["name"], "capability": Capability.FS_DELETE.value,
                                 "risk": RiskLevel.HIGH.name, "reason": "server marks the tool destructive"})
        server.permissions = sorted(required)
        server.state = MCPServerState.AWAITING_PERMISSIONS
        self._save(server)
        report = {"server": name, "tools": len(server.tools), "requested_permissions": sorted(required),
                  "findings": findings, "high_risk": any(item["risk"] == "HIGH" for item in findings)}
        self._event("scanned", name, {"permissions": sorted(required), "findings": len(findings)},
                    actor, RiskLevel.MEDIUM if findings else RiskLevel.LOW)
        return report

    def request_permissions(self, name: str, permissions: list[str], *, actor: str = "owner",
                            approval_id: str = "") -> MCPServer:
        """The owner decides which of the requested permissions are granted (install gate)."""
        server = self.get(name)
        if server.state not in (MCPServerState.AWAITING_PERMISSIONS, MCPServerState.SCANNED,
                                MCPServerState.INSPECTED, MCPServerState.DISABLED):
            raise ConflictError(f"server {name!r} is {server.state.value}; inspect and scan it first")
        requested = set(server.permissions) | set(permissions)
        known = {capability.value for capability in Capability}
        unknown = sorted(requested - known)
        if unknown:
            raise ConflictError(f"unknown permissions requested: {unknown}")
        if not actor.startswith("owner"):
            if not approval_id:
                raise ApprovalRequired("granting MCP server permissions requires owner approval",
                                       permissions=sorted(requested))
        granted = sorted(set(permissions or requested))
        server.permissions = granted
        server.trust = "owner_approved"
        server.state = MCPServerState.INSTALLED
        server.installed_at = iso()
        server.enabled = False
        self._save(server)
        self._event("permissions_granted", name, {"granted": granted, "approval_id": approval_id}, actor,
                    RiskLevel.MEDIUM)
        return server

    async def install(self, name: str, *, actor: str = "owner", approval_id: str = "",
                      permissions: list[str] | None = None) -> MCPServer:
        """Inspect + scan + grant permissions in one owner-driven flow."""
        server = await self.inspect(name, actor=actor)
        self.scan(name, actor=actor)
        return self.request_permissions(name, permissions or self.get(name).permissions, actor=actor,
                                        approval_id=approval_id)

    async def enable(self, name: str, *, actor: str = "owner") -> MCPServer:
        server = self.get(name)
        if server.state not in (MCPServerState.INSTALLED, MCPServerState.DISABLED):
            raise ConflictError(f"server {name!r} must be installed before it can be enabled "
                                f"(state: {server.state.value})")
        stale = self._clients.pop(name, None)
        if stale is not None:
            await self._close_client(stale)
        client = server.build_client()
        await client.connect()
        tools = await client.list_tools()
        self._clients[name] = client
        server.tools = [tool.to_dict() for tool in tools]
        server.enabled = True
        server.state = MCPServerState.ENABLED
        server.last_seen = iso()
        self._save(server)
        if self.tools is not None:
            self._register_tools(server, tools)
        self._event("enabled", name, {"tools": [tool.name for tool in tools]}, actor, RiskLevel.MEDIUM)
        return server

    async def disable(self, name: str, *, actor: str = "owner") -> MCPServer:
        server = self.get(name)
        client = self._clients.pop(name, None)
        if client is not None:
            await self._close_client(client)
        server.enabled = False
        server.state = MCPServerState.DISABLED
        self._save(server)
        if self.tools is not None:
            for tool in server.tools:
                try:
                    self.tools.unregister(f"mcp__{server.name}__{tool['name']}")
                except Exception:
                    pass
        self._event("disabled", name, {}, actor)
        return server

    async def uninstall(self, name: str, *, actor: str = "owner") -> None:
        server = self.get(name)
        if server.enabled:
            await self.disable(name, actor=actor)
        server.state = MCPServerState.UNINSTALLED
        server.permissions = []
        server.tools = []
        server.enabled = False
        self._save(server)
        self._event("uninstalled", name, {}, actor, RiskLevel.MEDIUM)

    async def health(self, name: str) -> dict[str, Any]:
        server = self.get(name)
        if not server.enabled:
            return {"server": name, "ok": False, "state": server.state.value, "healthy": False,
                    "reason": "not enabled", "tools": len(server.tools)}
        try:
            client = await self._client_for(server)
        except Exception as exc:
            return {"server": name, "ok": False, "state": server.state.value, "healthy": False,
                    "reason": f"{type(exc).__name__}: {exc}", "tools": len(server.tools)}
        healthy = await client.ping()
        return {"server": name, "ok": bool(healthy), "state": server.state.value, "healthy": healthy,
                "server_info": server.server_info, "tools": len(server.tools)}

    async def _close_client(self, client: Any) -> None:
        """Close a session, or reap its process directly when its loop is already gone."""
        if getattr(client, "usable_here", lambda: False)():
            try:
                await client.close()
                return
            except Exception:
                pass
        transport = getattr(client, "transport", None)
        kill = getattr(transport, "kill_now", None)
        if callable(kill):
            kill()

    async def _client_for(self, server: MCPServer) -> MCPClient:
        """Return a live session for *server*, reconnecting when the cached one is unusable.

        A session belongs to the event loop that created it. If a caller reaches the registry from a
        different loop (a sync CLI path, a second ``asyncio.run``, a thread), reusing that session
        would hang until the request timed out - so it is replaced instead of trusted.
        """
        client = self._clients.get(server.name)
        if client is not None and not client.usable_here():
            await self._close_client(client)
            self._clients.pop(server.name, None)
            client = None
        if client is None:
            client = server.build_client()
            await client.connect()
            await client.list_tools()
            if not client.usable_here():
                # Last resort: a loop-less thread cannot own a session; fail loudly instead of
                # handing back something that will hang.
                raise MCPError(
                    f"could not establish a usable MCP session for {server.name!r} on this event loop"
                )
            self._clients[server.name] = client
        return client

    # ------------------------------------------------------------------ tools
    def _register_tools(self, server: MCPServer, tools: list[MCPTool]) -> None:
        from ..tools.base import FunctionTool
        from ..security.validation import Schema

        for tool in tools:
            qualified = f"mcp__{server.name}__{tool.name}"
            capability = self._capability_for(tool)
            if capability.value not in server.permissions:
                # The owner did not grant this capability: the tool stays unregistered.
                continue

            def make_handler(server_name: str, tool_name: str) -> Any:
                async def handler(arguments: dict[str, Any], context: Any) -> Any:
                    from ..tools.base import ToolResult

                    result = await self.call(server_name, tool_name, arguments)
                    if result.get("is_error"):
                        return ToolResult.failure(result.get("text", "MCP tool reported an error"),
                                                  server=server_name, tool=tool_name)
                    return ToolResult.success(result, server=server_name, tool=tool_name)
                return handler

            self.tools.register(
                FunctionTool(
                    qualified, make_handler(server.name, tool.name),
                    description=f"[MCP:{server.name}] {tool.description or tool.name}. Output is untrusted "
                                "external content.",
                    capability=capability, risk=self._risk_for(capability),
                    schema=self._schema_from_json_schema(tool.input_schema),
                    requires_approval=capability in (Capability.SHELL_EXEC, Capability.CODE_EXEC,
                                                     Capability.FS_WRITE, Capability.FS_DELETE,
                                                     Capability.CREDENTIAL_USE, Capability.NET_SOCKET),
                    timeout_seconds=DEFAULT_TIMEOUT * 2,
                    tags=("mcp", server.name, tool.name),
                )
            )
            if hasattr(self.tools, "spec"):
                spec = self.tools.spec(qualified)
                spec.source = "mcp"
                spec.metadata = {"server": server.name, "tool": tool.name, "untrusted": True}

    @staticmethod
    def _capability_for(tool: MCPTool) -> Capability:
        name_lower = tool.name.lower()
        for hint, capability in CAPABILITY_HINTS.items():
            if hint in name_lower:
                return capability
        return Capability.MCP_EXECUTE

    @staticmethod
    def _risk_for(capability: Capability) -> RiskLevel:
        return {
            Capability.SHELL_EXEC: RiskLevel.HIGH, Capability.CODE_EXEC: RiskLevel.HIGH,
            Capability.FS_WRITE: RiskLevel.MEDIUM, Capability.FS_DELETE: RiskLevel.HIGH,
            Capability.CREDENTIAL_USE: RiskLevel.HIGH, Capability.BROWSER_CONTROL: RiskLevel.HIGH,
            Capability.NET_HTTP: RiskLevel.MEDIUM, Capability.NET_SOCKET: RiskLevel.MEDIUM,
            Capability.MCP_EXECUTE: RiskLevel.MEDIUM,
        }.get(capability, RiskLevel.MEDIUM)

    @staticmethod
    def _schema_from_json_schema(schema: dict[str, Any]) -> Any:
        from ..security.validation import Schema

        properties: dict[str, Schema] = {}
        required = list(schema.get("required") or [])
        for name, definition in (schema.get("properties") or {}).items():
            kind = definition.get("type", "string")
            if kind == "string":
                properties[name] = Schema.string(max_length=100_000, description=definition.get("description", ""))
            elif kind == "integer":
                properties[name] = Schema.integer(minimum=definition.get("minimum"),
                                                  maximum=definition.get("maximum"))
            elif kind == "number":
                properties[name] = Schema.number(minimum=definition.get("minimum"),
                                                 maximum=definition.get("maximum"))
            elif kind == "boolean":
                properties[name] = Schema.boolean()
            elif kind == "array":
                properties[name] = Schema.array(Schema.any())
            elif kind == "object":
                properties[name] = Schema.object({}, additional=True)
            else:
                properties[name] = Schema.any()
        return Schema.object(properties, required=required, additional=True)

    async def call(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool on an enabled server, with the output treated as untrusted data."""
        server = self.get(server_name)
        if not server.enabled or server.state is not MCPServerState.ENABLED:
            raise MCPError(f"MCP server {server_name!r} is not enabled")
        client = await self._client_for(server)
        result = await client.call_tool(tool_name, arguments)
        self._event("tool_call", server_name, {"tool": tool_name, "suspicious": result["suspicious"]},
                    "model:main", RiskLevel.MEDIUM if result["suspicious"] else RiskLevel.LOW)
        return result

    async def close(self) -> None:
        for name, client in list(self._clients.items()):
            await self._close_client(client)
        self._clients.clear()

    def _event(self, action: str, server: str, payload: dict[str, Any], actor: str,
               risk: RiskLevel = RiskLevel.LOW) -> None:
        self.log.append(EventKind.MCP, {"action": action, "server": server, **payload}, actor=actor,
                        source="mcp.registry", risk=risk)


_REGISTRY: MCPServerRegistry | None = None
_LOCK = threading.Lock()


def get_mcp_registry(**kwargs: Any) -> MCPServerRegistry:
    global _REGISTRY
    with _LOCK:
        if _REGISTRY is None:
            _REGISTRY = MCPServerRegistry(**kwargs)
        return _REGISTRY


def reset_mcp_registry() -> None:
    global _REGISTRY
    with _LOCK:
        _REGISTRY = None
