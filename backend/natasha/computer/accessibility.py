"""The accessibility tree: what the screen means, not only what it looks like.

A screenshot tells a model where pixels are; the accessibility tree tells it what those pixels
*are* - buttons, fields, menus and their names. Natasha prefers the tree when it is available and
falls back to window-level geometry, and when a platform gives nothing it says so instead of
inventing elements.

Backends, in order of preference:

* Linux  - AT-SPI via ``pyatspi``; otherwise ``wmctrl``/``xdotool`` window geometry.
* macOS  - System Events through ``osascript`` (needs Accessibility permission).
* Windows- UI Automation through PowerShell (``System.Windows.Automation``).
* Neither - an honest ``{"ok": false, "error": ...}``.

Every returned node is data, never authority: it is read-only, and callers wrap it in untrusted
content before a model sees it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Any

MAX_NODES = 400
TIMEOUT_SECONDS = 20.0


def _run(command: list[str], timeout: float = TIMEOUT_SECONDS) -> tuple[int, str, str]:
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return process.returncode, process.stdout, process.stderr
    except FileNotFoundError:
        return 127, "", f"{command[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{command[0]} timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, "", f"{type(exc).__name__}: {exc}"


def _node(role: str, name: str = "", value: str = "", bounds: Any = None, depth: int = 0,
          extra: dict[str, Any] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"role": role, "name": str(name)[:300], "value": str(value)[:300],
                            "bounds": bounds, "depth": depth}
    if extra:
        node.update(extra)
    return node


def _linux_atspi() -> dict[str, Any] | None:
    try:
        import pyatspi  # type: ignore
    except Exception:
        return None
    nodes: list[dict[str, Any]] = []

    def walk(accessible: Any, depth: int) -> None:
        if len(nodes) >= MAX_NODES or depth > 12:
            return
        try:
            role = accessible.getRoleName()
            name = accessible.name or ""
            value = ""
            try:
                text = accessible.queryText()
                value = text.getText(0, text.characterCount)
            except Exception:
                value = ""
            bounds = None
            try:
                extents = accessible.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                bounds = [extents.x, extents.y, extents.width, extents.height]
            except Exception:
                bounds = None
            if role != "unknown":
                nodes.append(_node(role, name, value, bounds, depth))
            for index in range(accessible.childCount):
                try:
                    walk(accessible.getChildAtIndex(index), depth + 1)
                except Exception:
                    continue
        except Exception:
            return

    try:
        desktop = pyatspi.Registry.getDesktop(0)
        for index in range(desktop.childCount):
            walk(desktop.getChildAtIndex(index), 0)
    except Exception as exc:
        return {"ok": False, "platform": "linux", "backend": "atspi", "nodes": [],
                "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": bool(nodes), "platform": "linux", "backend": "atspi", "nodes": nodes,
            "error": "" if nodes else "AT-SPI returned no nodes"}


def _linux_windows() -> dict[str, Any] | None:
    """Window-level fallback: real geometry, no widget detail."""
    tool = shutil.which("wmctrl") or shutil.which("xdotool")
    if tool is None:
        return None
    nodes: list[dict[str, Any]] = []
    if tool.endswith("wmctrl"):
        code, out, _ = _run(["wmctrl", "-lG"])
        for line in out.splitlines():
            parts = line.split(None, 7)
            if len(parts) >= 8:
                bounds = [int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])]
                nodes.append(_node("window", parts[7], bounds=bounds))
    else:
        code, out, _ = _run(["xdotool", "search", "--name", ""])
        for window_id in out.split()[:MAX_NODES]:
            code, name, _ = _run(["xdotool", "getwindowname", window_id])
            _, geometry, _ = _run(["xdotool", "getwindowgeometry", "--shell", window_id])
            values = dict(
                line.split("=", 1) for line in geometry.splitlines() if "=" in line
            )
            bounds = [int(values.get("X", 0)), int(values.get("Y", 0)),
                      int(values.get("WIDTH", 0)), int(values.get("HEIGHT", 0))]
            nodes.append(_node("window", name.strip(), bounds=bounds, extra={"id": window_id}))
    return {"ok": bool(nodes), "platform": "linux", "backend": "wmctrl/xdotool", "nodes": nodes,
            "error": "" if nodes else "no windows reported (is $DISPLAY set?)"}


_MACOS_SCRIPT = r'''
tell application "System Events"
  set out to ""
  repeat with proc in (application processes whose visible is true)
    try
      set pname to name of proc
      repeat with win in (windows of proc)
        try
          set wname to name of win
          set wpos to position of win
          set wsz to size of win
          set out to out & pname & tab & wname & tab & (item 1 of wpos) & tab & (item 2 of wpos) & tab & (item 1 of wsz) & tab & (item 2 of wsz) & tab & "window" & linefeed
          repeat with el in (UI elements of win)
            try
              set rname to name of el
              set rrole to role of el
              set rpos to position of el
              set rsz to size of el
              set out to out & pname & tab & rname & tab & (item 1 of rpos) & tab & (item 2 of rpos) & tab & (item 1 of rsz) & tab & (item 2 of rsz) & tab & rrole & linefeed
            end try
          end repeat
        end try
      end repeat
    end try
  end repeat
  return out
end tell
'''


def _macos() -> dict[str, Any]:
    if shutil.which("osascript") is None:
        return {"ok": False, "platform": "darwin", "backend": "osascript", "nodes": [],
                "error": "osascript not found"}
    code, out, err = _run(["osascript", "-e", _MACOS_SCRIPT], timeout=30.0)
    if code != 0:
        return {"ok": False, "platform": "darwin", "backend": "osascript", "nodes": [],
                "error": (err or out).strip()[:300] or "osascript failed"}
    nodes = []
    for line in out.splitlines()[:MAX_NODES]:
        parts = line.split("\t")
        if len(parts) >= 7:
            app, name, x, y, width, height, role = parts[:7]
            try:
                bounds = [int(float(x)), int(float(y)), int(float(width)), int(float(height))]
            except ValueError:
                bounds = None
            nodes.append(_node(role or "element", name, bounds=bounds,
                               extra={"application": app}))
    return {"ok": bool(nodes), "platform": "darwin", "backend": "osascript", "nodes": nodes,
            "error": "" if nodes else "no accessible elements (grant Accessibility permission)"}


_WINDOWS_SCRIPT = r'''
Add-Type -AssemblyName UIAutomationClient
$root = [System.Windows.Automation.AutomationElement]::RootElement
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
function Walk($element, $depth) {
  if ($element -eq $null -or $depth -gt 6) { return }
  try {
    $name = $element.Current.Name
    $role = $element.Current.ControlType.ProgrammaticName
    $rect = $element.Current.BoundingRectangle
    $bounds = "$([int]$rect.X),$([int]$rect.Y),$([int]$rect.Width),$([int]$rect.Height)"
    Write-Output "$role`t$name`t$bounds`t$depth"
  } catch {}
  $child = $walker.GetFirstChild($element)
  while ($child -ne $null) {
    Walk $child ($depth + 1)
    $child = $walker.GetNextSibling($child)
  }
}
Walk $root 0
'''


def _windows() -> dict[str, Any]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        return {"ok": False, "platform": "win32", "backend": "uiautomation", "nodes": [],
                "error": "powershell not found"}
    code, out, err = _run([powershell, "-NoProfile", "-Command", _WINDOWS_SCRIPT], timeout=60.0)
    if code != 0:
        return {"ok": False, "platform": "win32", "backend": "uiautomation", "nodes": [],
                "error": (err or out).strip()[:300] or "UI Automation failed"}
    nodes = []
    for line in out.splitlines()[:MAX_NODES]:
        parts = line.split("\t")
        if len(parts) >= 4:
            role, name, bounds_raw, depth = parts[:4]
            bounds = None
            try:
                bounds = [int(float(value)) for value in bounds_raw.split(",")[:4]]
            except ValueError:
                bounds = None
            nodes.append(_node(role.replace("ControlType.", ""), name, bounds=bounds,
                               depth=int(depth) if depth.isdigit() else 0))
    return {"ok": bool(nodes), "platform": "win32", "backend": "uiautomation", "nodes": nodes,
            "error": "" if nodes else "UI Automation returned no elements"}


def _empty(error: str, backend: str = "") -> dict[str, Any]:
    return {"ok": False, "platform": sys.platform, "backend": backend, "nodes": [],
            "count": 0, "error": error}


def get_accessibility_tree(*, limit: int = MAX_NODES) -> dict[str, Any]:
    """Read the accessibility tree of the current desktop. Never raises, never guesses.

    The shape is always the same - ``ok``, ``platform``, ``backend``, ``nodes``, ``count``, ``error`` -
    so a caller can branch on ``ok`` alone no matter which platform answered.
    """
    try:
        result: dict[str, Any] | None
        if sys.platform.startswith("linux"):
            result = _linux_atspi() or _linux_windows()
            if result is None:
                return _empty("no accessibility backend: install pyatspi (AT-SPI) or x11-utils "
                              "(xdotool/wmctrl)")
        elif sys.platform == "darwin":
            result = _macos()
        elif sys.platform.startswith("win"):
            result = _windows()
        else:
            return _empty(f"unsupported platform {sys.platform}")
    except Exception as exc:  # pragma: no cover - defensive
        return _empty(f"{type(exc).__name__}: {exc}")
    nodes = list(result.get("nodes", []))[:limit]
    normalized = {"ok": bool(result.get("ok")) and bool(nodes),
                  "platform": result.get("platform", sys.platform),
                  "backend": result.get("backend", ""),
                  "nodes": nodes,
                  "count": len(nodes),
                  "error": result.get("error", "") if nodes else (result.get("error") or "no elements")}
    return normalized


def summarise_tree(tree: dict[str, Any], *, limit: int = 120) -> list[str]:
    """Flatten a tree into model-readable lines, clipped to something a prompt can hold."""
    lines: list[str] = []
    for node in tree.get("nodes", [])[:limit]:
        name = node.get("name") or node.get("value") or ""
        line = f"- {node.get('role', 'element')}: {name}".rstrip(": ")
        bounds = node.get("bounds")
        if bounds:
            line += f" @{bounds[0]},{bounds[1]} {bounds[2]}x{bounds[3]}"
        lines.append(line)
    return lines


def tree_as_json(tree: dict[str, Any]) -> str:
    return json.dumps(tree, ensure_ascii=False, default=str)
