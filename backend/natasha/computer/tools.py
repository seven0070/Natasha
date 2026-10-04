"""Tool wrappers for computer and browser control.

These are the tools a model can call. Each one delegates to a gated controller, so the policy engine
and the approval engine (not the model, and not the prompt) decide what actually happens.
"""

from __future__ import annotations

from typing import Any

from ..core.risk import RiskLevel
from ..security.policy import Capability
from ..security.validation import Schema
from ..tools.base import ToolResult

#: Registered names -> (capability, risk, whether the owner must approve first).
COMPUTER_TOOLS: dict[str, tuple[str, RiskLevel, bool]] = {
    "computer_screenshot": ("take a screenshot of the screen", RiskLevel.MEDIUM, False),
    "computer_click": ("click at screen coordinates", RiskLevel.HIGH, True),
    "computer_move": ("move the mouse pointer", RiskLevel.MEDIUM, False),
    "computer_type": ("type text with the keyboard", RiskLevel.HIGH, True),
    "computer_key": ("press a key or key combination", RiskLevel.HIGH, True),
    "computer_scroll": ("scroll the screen", RiskLevel.MEDIUM, False),
    "computer_clipboard_read": ("read the clipboard", RiskLevel.HIGH, True),
    "computer_clipboard_write": ("write to the clipboard", RiskLevel.MEDIUM, False),
    "computer_window_focus": ("bring a window to the front", RiskLevel.MEDIUM, True),
    "computer_launch": ("launch an application", RiskLevel.HIGH, True),
}


def register_computer_tools(registry: Any, controller: Any, browser: Any = None) -> list[str]:
    """Register desktop tools (and browser tools when a controller is supplied)."""
    from ..tools.base import FunctionTool

    registered: list[str] = []

    async def screenshot(arguments: dict[str, Any], context: Any) -> ToolResult:
        record = controller.screenshot(name=arguments.get("name", ""), actor=context.actor)
        return ToolResult(record["ok"], output=record, error=record["error"],
                          artifacts=[record["artifact"]] if record.get("artifact") else [])

    async def click(arguments: dict[str, Any], context: Any) -> ToolResult:
        record = controller.click(int(arguments["x"]), int(arguments["y"]),
                                  button=arguments.get("button", "left"),
                                  clicks=int(arguments.get("clicks", 1)), actor=context.actor,
                                  approval_id=context.extra.get("approval_id", ""))
        return ToolResult(record["ok"], output=record, error=record["error"])

    async def move(arguments: dict[str, Any], context: Any) -> ToolResult:
        record = controller.move(int(arguments["x"]), int(arguments["y"]), actor=context.actor)
        return ToolResult(record["ok"], output=record, error=record["error"])

    async def type_text(arguments: dict[str, Any], context: Any) -> ToolResult:
        record = controller.type_text(str(arguments.get("text", "")), actor=context.actor,
                                      approval_id=context.extra.get("approval_id", ""))
        return ToolResult(record["ok"], output=record, error=record["error"])

    async def key(arguments: dict[str, Any], context: Any) -> ToolResult:
        record = controller.press(str(arguments.get("key", "")), presses=int(arguments.get("presses", 1)),
                                  actor=context.actor, approval_id=context.extra.get("approval_id", ""))
        return ToolResult(record["ok"], output=record, error=record["error"])

    async def scroll(arguments: dict[str, Any], context: Any) -> ToolResult:
        record = controller.scroll(int(arguments.get("amount", 3)), actor=context.actor)
        return ToolResult(record["ok"], output=record, error=record["error"])

    async def clipboard_read(arguments: dict[str, Any], context: Any) -> ToolResult:
        record = controller.clipboard_read(actor=context.actor)
        return ToolResult(record["ok"], output=record, error=record["error"], redacted=True)

    async def clipboard_write(arguments: dict[str, Any], context: Any) -> ToolResult:
        record = controller.clipboard_write(str(arguments.get("text", "")), actor=context.actor)
        return ToolResult(record["ok"], output=record, error=record["error"])

    async def window_focus(arguments: dict[str, Any], context: Any) -> ToolResult:
        record = controller.raise_window(str(arguments.get("title", "")), actor=context.actor,
                                         approval_id=context.extra.get("approval_id", ""))
        return ToolResult(record["ok"], output=record, error=record["error"])

    async def launch(arguments: dict[str, Any], context: Any) -> ToolResult:
        command = arguments.get("command") or ""
        record = controller.launch(command, actor=context.actor,
                                   approval_id=context.extra.get("approval_id", ""))
        return ToolResult(record["ok"], output=record, error=record["error"])

    handlers = {"computer_screenshot": screenshot, "computer_click": click, "computer_move": move,
                "computer_type": type_text, "computer_key": key, "computer_scroll": scroll,
                "computer_clipboard_read": clipboard_read, "computer_clipboard_write": clipboard_write,
                "computer_window_focus": window_focus, "computer_launch": launch}
    schemas = {
        "computer_screenshot": Schema.object({"name": Schema.string(description="file name")}),
        "computer_click": Schema.object({"x": Schema.integer(description="x coordinate"),
                                         "y": Schema.integer(description="y coordinate"),
                                         "button": Schema.enum_of(["left", "middle", "right"]),
                                         "clicks": Schema.integer(description="click count")},
                                        required=["x", "y"]),
        "computer_move": Schema.object({"x": Schema.integer(description="x coordinate"),
                                        "y": Schema.integer(description="y coordinate")},
                                       required=["x", "y"]),
        "computer_type": Schema.object({"text": Schema.string(description="text to type")}, required=["text"]),
        "computer_key": Schema.object({"key": Schema.string(description="key name, e.g. 'enter' or 'ctrl+s'"),
                                       "presses": Schema.integer(description="repeat count")}, required=["key"]),
        "computer_scroll": Schema.object({"amount": Schema.integer(description="positive scrolls up")}),
        "computer_clipboard_read": Schema.object({}),
        "computer_clipboard_write": Schema.object({"text": Schema.string(description="text for the clipboard")},
                                                  required=["text"]),
        "computer_window_focus": Schema.object({"title": Schema.string(description="window title substring")},
                                               required=["title"]),
        "computer_launch": Schema.object({"command": Schema.string(description="executable plus arguments")},
                                         required=["command"]),
    }
    capabilities = {"computer_screenshot": Capability.SCREEN_CAPTURE, "computer_click": Capability.INPUT_CONTROL,
                    "computer_move": Capability.INPUT_CONTROL, "computer_type": Capability.INPUT_CONTROL,
                    "computer_key": Capability.INPUT_CONTROL, "computer_scroll": Capability.INPUT_CONTROL,
                    "computer_clipboard_read": Capability.CLIPBOARD_READ,
                    "computer_clipboard_write": Capability.CLIPBOARD_WRITE,
                    "computer_window_focus": Capability.WINDOW_CONTROL,
                    "computer_launch": Capability.APP_CONTROL}
    for name, description in COMPUTER_TOOLS.items():
        registry.register(FunctionTool(name, handlers[name], description=description[0],
                                       capability=capabilities[name], risk=description[1],
                                       schema=schemas[name], requires_approval=description[2],
                                       resource_field="name" if name == "computer_screenshot" else ""))
        registered.append(name)

    if browser is not None:
        registered.extend(_register_browser_tools(registry, browser))
    return registered


def _register_browser_tools(registry: Any, browser: Any) -> list[str]:
    from ..tools.base import FunctionTool
    from ..security.validation import Schema as S

    async def open_page(arguments: dict[str, Any], context: Any) -> ToolResult:
        page = browser.open(str(arguments["url"]), actor=context.actor)
        # Page text is external content: it goes back fenced, with injection findings attached.
        return ToolResult(True, output={"url": page["url"], "title": page["title"],
                                        "status": page["status"],
                                        "text": page["text"][: int(arguments.get("max_chars", 20000))],
                                        "links": page["links"][:50], "backend": page["backend"]},
                          metadata={"suspicious": page["suspicious"], "findings": page["findings"],
                                    "trust": "external"})

    async def search(arguments: dict[str, Any], context: Any) -> ToolResult:
        result = browser.search(str(arguments["query"]), engine=arguments.get("engine", "duckduckgo"),
                               actor=context.actor)
        return ToolResult(True, output=result,
                          metadata={"suspicious": result["suspicious"], "findings": result["findings"]})

    async def click(arguments: dict[str, Any], context: Any) -> ToolResult:
        page = browser.click(str(arguments["url"]), str(arguments["selector"]), actor=context.actor,
                             approval_id=context.extra.get("approval_id", ""))
        return ToolResult(True, output={"url": page["url"], "title": page["title"],
                                        "text": page["text"][:5000]},
                          metadata={"suspicious": page["suspicious"], "findings": page["findings"]})

    async def fill(arguments: dict[str, Any], context: Any) -> ToolResult:
        page = browser.fill(str(arguments["url"]), str(arguments["selector"]),
                            str(arguments["value"]), actor=context.actor,
                            approval_id=context.extra.get("approval_id", ""))
        return ToolResult(True, output={"url": page["url"], "title": page["title"]},
                          metadata={"suspicious": page["suspicious"]}, redacted=True)

    async def screenshot(arguments: dict[str, Any], context: Any) -> ToolResult:
        record = browser.screenshot(str(arguments["url"]), name=arguments.get("name", ""),
                                    actor=context.actor)
        return ToolResult(True, output=record, artifacts=[record["path"]])

    async def extract(arguments: dict[str, Any], context: Any) -> ToolResult:
        """Fetch and read a page, returning the readable text (no markdown parser required)."""
        page = browser.read(str(arguments["url"]), max_chars=int(arguments.get("max_chars", 20000)),
                            actor=context.actor)
        return ToolResult(True, output={"url": page["url"], "title": page["title"], "text": page["text"]},
                          metadata={"suspicious": page["suspicious"], "findings": page["findings"],
                                    "trust": "external"})

    tools = {
        "browser_open": (open_page, "open a URL and return its rendered text and links"),
        "browser_search": (search, "search the web and return result links"),
        "browser_click": (click, "follow a link or click an element by selector"),
        "browser_fill": (fill, "fill a form field"),
        "browser_screenshot": (screenshot, "screenshot a web page"),
        "browser_extract": (extract, "read a web page's readable text"),
    }
    from ..core.risk import RiskLevel as R

    schemas = {
        "browser_open": S.object({"url": S.string(description="url to open"),
                                  "max_chars": S.integer(description="text limit")}, required=["url"]),
        "browser_search": S.object({"query": S.string(description="search query"),
                                    "engine": S.enum_of(["duckduckgo", "wikipedia"])}, required=["query"]),
        "browser_click": S.object({"url": S.string(description="page url"),
                                   "selector": S.string(description="css selector or link text")},
                                  required=["url", "selector"]),
        "browser_fill": S.object({"url": S.string(description="page url"),
                                  "selector": S.string(description="css selector"),
                                  "value": S.string(description="value to type")},
                                 required=["url", "selector", "value"]),
        "browser_screenshot": S.object({"url": S.string(description="page url"),
                                        "name": S.string(description="file name")}, required=["url"]),
        "browser_extract": S.object({"url": S.string(description="page url"),
                                     "max_chars": S.integer(description="text limit")}, required=["url"]),
    }
    # Clicking and filling mutate remote state; both need the owner (or an explicit approval id).
    approval_required = {"browser_click", "browser_fill"}
    for name, (handler, description) in tools.items():
        registry.register(FunctionTool(name, handler, description=description,
                                       capability=Capability.BROWSER_CONTROL,
                                       risk=R.HIGH if name in approval_required else R.MEDIUM,
                                       schema=schemas[name], requires_approval=name in approval_required,
                                       resource_field="url"))
    return sorted(tools)
