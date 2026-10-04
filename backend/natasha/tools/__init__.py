"""Tools: every privileged action Natasha can take, behind one audited choke point.

A tool declares the capability it needs, its risk and a schema. ``ToolRegistry.execute`` then runs
five gates in order - argument validation, policy, approval, execution with a timeout, and output
validation - so a model that calls a tool cannot skip any of them. Tool output is treated as
external content: bounded, sanitised and never authoritative.
"""

from .base import FunctionTool, Tool, ToolContext, ToolResult, ToolSpec
from .registry import ToolRegistry, get_tool_registry
from .builtin import builtin_tools, register_builtins

__all__ = [
    "FunctionTool", "Tool", "ToolContext", "ToolResult", "ToolSpec", "ToolRegistry",
    "get_tool_registry", "builtin_tools", "register_builtins",
]
