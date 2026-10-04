"""Computer and browser control.

Two controllers, one contract:

* :class:`ComputerController` - the desktop: screenshots, mouse, keyboard, apps, clipboard, windows.
* :class:`BrowserController`  - the web: navigate, read, click, fill, screenshot, download.

Both are *gated*: every action is checked against the policy engine (and the approval engine when the
risk demands it) before it touches the machine, and every action is written to the append-only event
log. Backends are discovered honestly - if the host has no automation backend, the controller says so
instead of pretending to have clicked something.
"""

from .accessibility import get_accessibility_tree, summarise_tree, tree_as_json
from .controller import (
    BackendUnavailable,
    ComputerAction,
    ComputerController,
    ComputerUnavailable,
    ShellScreenBackend,
    get_computer_controller,
)
from .browser import (
    BrowserController,
    BrowserPage,
    HttpClientBackend,
    PlaywrightBackend,
    get_browser_controller,
)

__all__ = [
    "get_accessibility_tree", "summarise_tree", "tree_as_json",
    "BackendUnavailable", "ComputerAction", "ComputerController", "ComputerUnavailable",
    "ShellScreenBackend", "get_computer_controller",
    "BrowserController", "BrowserPage", "HttpClientBackend", "PlaywrightBackend",
    "get_browser_controller",
]
