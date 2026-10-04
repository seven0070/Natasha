"""A dependency-free MCP client (JSON-RPC 2.0) over stdio or streamable HTTP."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from typing import Any

from ..core import MCPError
from ..security.injection import ContentTrust, ExternalContent

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 30.0


@dataclass
class MCPTool:
    """A tool exposed by an MCP server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    server: str = ""
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        return f"mcp__{self.server}__{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "qualified_name": self.qualified_name, "server": self.server,
                "description": self.description, "input_schema": self.input_schema,
                "annotations": self.annotations}


class MCPTransport:
    """Transport interface."""

    async def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        raise NotImplementedError

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response)."""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class StdioTransport(MCPTransport):
    """Speaks newline-delimited JSON-RPC over a child process's stdin/stdout."""

    def __init__(self, command: str, args: list[str] | None = None, *, env: dict[str, str] | None = None,
                 cwd: str = "", timeout: float = DEFAULT_TIMEOUT) -> None:
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.cwd = cwd
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        #: The stderr drain task is kept so it can be cancelled on close. Dropping it leaves a task
        #: holding the child's pipe open - it outlives the event loop and shows up as an unraisable
        #: "Event loop is closed" error plus unclosed-transport warnings.
        self._stderr_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.stderr_tail: list[str] = []

    async def start(self) -> None:
        if self._process is not None:
            return
        resolved = shutil.which(self.command) or self.command
        environment = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                       "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8", **self.env}
        try:
            self._process = await asyncio.create_subprocess_exec(
                resolved, *self.args, cwd=self.cwd or None, env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise MCPError(f"MCP server command not found: {self.command}") from exc
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    continue
                message_id = message.get("id")
                if message_id is not None and message_id in self._pending:
                    future = self._pending.pop(message_id)
                    if not future.done():
                        future.set_result(message)
        finally:
            # EOF means the server is gone. Waiting out the request timeout would turn a crashed
            # server into a 30-second hang, so every pending request fails right now.
            self._fail_pending(self._exit_reason())

    def _exit_reason(self) -> str:
        process = self._process
        code = None if process is None else process.returncode
        tail = " | ".join(self.stderr_tail[-3:]) if self.stderr_tail else ""
        detail = f"exit code {code}" if code is not None else "the stream closed"
        return f"MCP server {self.command!r} stopped ({detail})" + (f": {tail}" if tail else "")

    def _fail_pending(self, reason: str) -> None:
        for request_id, future in list(self._pending.items()):
            self._pending.pop(request_id, None)
            if not future.done():
                future.set_exception(MCPError(reason))

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            self.stderr_tail.append(line.decode("utf-8", "replace").rstrip())
            self.stderr_tail[:] = self.stderr_tail[-40:]

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        await self.start()
        assert self._process is not None and self._process.stdin is not None
        async with self._lock:
            self._next_id += 1
            request_id = self._next_id
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            self._process.stdin.write((json.dumps(payload) + "\n").encode())
            await self._process.stdin.drain()
        try:
            message = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise MCPError(f"MCP request {method!r} timed out after {timeout}s",
                           stderr="\n".join(self.stderr_tail[-5:])) from exc
        if "error" in message:
            error = message["error"]
            raise MCPError(f"MCP error from {self.command}: {error.get('message', error)}",
                           code=error.get("code"))
        return message.get("result", {})

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.start()
        assert self._process is not None and self._process.stdin is not None
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._process.stdin.write((json.dumps(payload) + "\n").encode())
        await self._process.stdin.drain()

    def kill_now(self) -> None:
        """Reap the child process without touching the event loop.

        Used when a session outlives the loop that created it: awaiting its transport there would
        raise "Future attached to a different loop", so the process is terminated directly.
        """
        process, self._process = self._process, None
        self._reader = None
        self._stderr_task = None
        if process is not None and process.returncode is None:
            for action in (process.terminate, process.kill):
                try:
                    action()
                except Exception:
                    continue
                break
        _close_pipes(process)

    async def close(self) -> None:
        reader, self._reader = self._reader, None
        stderr_task, self._stderr_task = self._stderr_task, None
        process, self._process = self._process, None
        try:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5)
        finally:
            self._process = None
            self._stderr_task = None
        for task in (reader, stderr_task):
            if task is not None:
                task.cancel()
        pending = [task for task in (reader, stderr_task) if task is not None]
        if pending:
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception:
                pass
        # Close the child's pipes: an asyncio subprocess transport that is only garbage collected
        # reports "unclosed transport" once the loop is gone.
        _close_pipes(process)


def _close_pipes(process: Any) -> None:
    """Best-effort close of a child's stdin/stdout/stderr transports (never raises)."""
    if process is None:
        return
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:
            continue


class StreamableHTTPTransport(MCPTransport):
    """MCP over HTTP (JSON responses or SSE streams), per the streamable-HTTP transport."""

    def __init__(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.url = url
        self.headers = {"content-type": "application/json", "accept": "application/json, text/event-stream",
                        **(headers or {})}
        self.timeout = timeout
        self._next_id = 0
        self._session_id = ""

    async def start(self) -> None:
        return None

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        import httpx

        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        headers = dict(self.headers)
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise MCPError(f"MCP HTTP request failed: {exc}") from exc
        if response.headers.get("mcp-session-id"):
            self._session_id = response.headers["mcp-session-id"]
        if response.status_code >= 400:
            raise MCPError(f"MCP HTTP {response.status_code}: {response.text[:300]}")
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            message = self._parse_sse(response.text)
        else:
            try:
                message = response.json()
            except json.JSONDecodeError as exc:
                raise MCPError(f"MCP returned non-JSON body: {response.text[:200]}") from exc
        if "error" in message:
            raise MCPError(str(message["error"]), code=message["error"].get("code"))
        return message.get("result", {})

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        import httpx

        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        headers = dict(self.headers)
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await client.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError:
            return  # notifications are best-effort

    async def close(self) -> None:
        return None

    @staticmethod
    def _parse_sse(text: str) -> dict[str, Any]:
        for block in text.split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data:
                        try:
                            return json.loads(data)
                        except json.JSONDecodeError:
                            continue
        raise MCPError("MCP SSE response contained no JSON data")


class MCPClient:
    """A connected MCP server: initialize, list, call, close."""

    def __init__(self, name: str, transport: MCPTransport, *, client_name: str = "natasha",
                 client_version: str = "1.0.0", timeout: float = DEFAULT_TIMEOUT) -> None:
        self.name = name
        self.transport = transport
        self.client_name = client_name
        self.client_version = client_version
        self.timeout = timeout
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self._tools: list[MCPTool] = []
        self._initialized = False

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> dict[str, Any]:
        result = await self.transport.request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION,
             "capabilities": {"roots": {"listChanged": False}, "sampling": {}},
             "clientInfo": {"name": self.client_name, "version": self.client_version}},
            timeout=self.timeout,
        )
        self.server_info = result.get("serverInfo", {}) or {}
        self.capabilities = result.get("capabilities", {}) or {}
        protocol = result.get("protocolVersion", PROTOCOL_VERSION)
        # ``notifications/initialized`` has no id, so it must be sent as a notification.
        await self.transport.notify("notifications/initialized", {})
        self._initialized = True
        from ..core.async_utils import current_loop_id

        self._loop_id = current_loop_id()
        return {"server": self.name, "protocol": protocol, "server_info": self.server_info,
                "capabilities": sorted(self.capabilities)}

    async def close(self) -> None:
        await self.transport.close()
        self._initialized = False
        self._loop_id = 0

    @property
    def bound_loop_id(self) -> int:
        """Identity of the loop this session lives on (0 when not connected)."""
        return getattr(self, "_loop_id", 0)

    def usable_here(self) -> bool:
        """True when the session can be used from the *currently running* loop.

        A session is bound to the loop that spawned its transport; using it from another loop would
        hang forever instead of failing, so callers must check this and reconnect.
        """
        if not self._initialized or not self.bound_loop_id:
            return False
        from ..core.async_utils import current_loop_id

        return current_loop_id() == self.bound_loop_id

    # ------------------------------------------------------------------ tools
    async def list_tools(self) -> list[MCPTool]:
        result = await self.transport.request("tools/list", {}, timeout=self.timeout)
        tools: list[MCPTool] = []
        for item in result.get("tools", []):
            tools.append(MCPTool(
                name=item.get("name", ""), description=item.get("description", ""),
                input_schema=item.get("inputSchema") or item.get("input_schema") or {},
                server=self.name, annotations=item.get("annotations") or {},
            ))
        self._tools = tools
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await self.transport.request(
            "tools/call", {"name": name, "arguments": arguments or {}}, timeout=self.timeout,
        )
        text_parts: list[str] = []
        for block in result.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif isinstance(block, dict):
                text_parts.append(json.dumps(block)[:2000])
        text = "\n".join(text_parts).strip()
        content = ExternalContent(text=text, source=f"mcp:{self.name}:{name}", trust=ContentTrust.EXTERNAL)
        return {
            "server": self.name, "tool": name, "is_error": bool(result.get("isError", False)),
            "text": content.render(), "raw_text": content.text, "suspicious": content.suspicious,
            "findings": [item["pattern"] for item in (content.report.findings if content.report else [])],
            "structured": result.get("structuredContent"),
        }

    # ------------------------------------------------------------------ resources
    async def list_resources(self) -> list[dict[str, Any]]:
        try:
            result = await self.transport.request("resources/list", {}, timeout=self.timeout)
        except MCPError:
            return []
        return [{"uri": item.get("uri", ""), "name": item.get("name", ""),
                 "mime_type": item.get("mimeType", ""), "description": item.get("description", "")}
                for item in result.get("resources", [])]

    async def read_resource(self, uri: str) -> str:
        result = await self.transport.request("resources/read", {"uri": uri}, timeout=self.timeout)
        parts = []
        for block in result.get("contents", []) or []:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("blob") or "")[:40_000])
        return "\n".join(parts)

    async def ping(self) -> bool:
        """Health probe that does not punish servers for omitting optional methods.

        ``ping`` is optional in MCP, so an error or silence there is not a health verdict: fall back
        to ``tools/list``, which every tool-serving MCP server must implement.
        """
        for method in ("ping", "tools/list"):
            try:
                await self.transport.request(method, {}, timeout=5)
                return True
            except Exception:
                continue
        return False

    def tool_by_name(self, name: str) -> MCPTool | None:
        for tool in self._tools:
            if tool.name == name:
                return tool
        return None
