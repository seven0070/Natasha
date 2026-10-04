"""Model Context Protocol runtime: connect to MCP servers, review what they offer, use their tools.

MCP servers are third-party code. Natasha therefore: never auto-installs one, shows the owner exactly
which tools a server exposes before enabling it, treats every response as untrusted content, and
namespaces those tools (``mcp__<server>__<tool>``) so they can never shadow a builtin.
"""

from .client import MCPClient, MCPTool, MCPTransport, StdioTransport, StreamableHTTPTransport
from .registry import MCPServer, MCPServerRegistry, MCPServerState, get_mcp_registry

__all__ = [
    "MCPClient", "MCPTool", "MCPTransport", "StdioTransport", "StreamableHTTPTransport",
    "MCPServer", "MCPServerRegistry", "MCPServerState", "get_mcp_registry",
]
