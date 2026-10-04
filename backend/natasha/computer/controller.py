"""Desktop control with pluggable, honestly-detected backends."""

from __future__ import annotations

import abc
import os
import shutil
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..core import AccessDenied, ApprovalRequired, NatashaError, new_id
from ..core.clock import iso
from ..core.risk import RiskLevel
from ..events import EventKind, get_event_log
from ..security.policy import Capability, PolicyRequest


class ComputerUnavailable(NatashaError):
    """No usable desktop automation backend on this machine."""


class BackendUnavailable(ComputerUnavailable):
    """A specific backend was requested but is not installed."""


@dataclass
class ComputerAction:
    """The auditable record of one desktop action."""

    id: str = field(default_factory=lambda: new_id("act"))
    kind: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    error: str = ""
    artifact: str = ""
    at: str = field(default_factory=iso)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "arguments": _redact(self.arguments), "ok": self.ok,
                "error": self.error, "artifact": self.artifact, "at": self.at,
                "duration_ms": round(self.duration_ms, 2)}


#: Argument names whose values must never reach the event log.
_SECRET_ARGUMENTS = {"text", "password", "secret", "content", "clipboard"}


def _redact(arguments: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in arguments.items():
        if key.lower() in _SECRET_ARGUMENTS and isinstance(value, str):
            redacted[key] = f"<{len(value)} chars redacted>"
        else:
            redacted[key] = value
    return redacted


class ScreenBackend(abc.ABC):
    """The narrow contract every desktop backend implements."""

    name = "abstract"

    @property
    def available(self) -> bool:
        return False

    def size(self) -> tuple[int, int]:
        raise BackendUnavailable(f"{self.name} backend is not available")

    def screenshot(self, path: str) -> str:
        raise BackendUnavailable(f"{self.name} backend is not available")

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        raise BackendUnavailable(f"{self.name} backend is not available")

    def move(self, x: int, y: int) -> None:
        raise BackendUnavailable(f"{self.name} backend is not available")

    def type_text(self, text: str) -> None:
        raise BackendUnavailable(f"{self.name} backend is not available")

    def press(self, key: str, *, presses: int = 1) -> None:
        raise BackendUnavailable(f"{self.name} backend is not available")

    def scroll(self, amount: int) -> None:
        raise BackendUnavailable(f"{self.name} backend is not available")

    def clipboard_read(self) -> str:
        raise BackendUnavailable(f"{self.name} backend does not support the clipboard")

    def clipboard_write(self, text: str) -> None:
        raise BackendUnavailable(f"{self.name} backend does not support the clipboard")

    def raise_window(self, title: str) -> None:
        raise BackendUnavailable(f"{self.name} backend cannot manage windows")

    def launch(self, command: list[str]) -> int:
        raise BackendUnavailable(f"{self.name} backend cannot launch applications")


class PyAutoGUIBackend(ScreenBackend):
    """Cross-platform backend used when ``pyautogui`` is installed."""

    name = "pyautogui"

    def __init__(self) -> None:
        self._gui: Any = None

    @property
    def available(self) -> bool:
        if self._gui is not None:
            return True
        try:
            import pyautogui  # type: ignore
        except Exception:
            return False
        self._gui = pyautogui
        return True

    def _require(self) -> Any:
        if not self.available:
            raise BackendUnavailable("pyautogui is not installed (python3 -m pip install --break-system-packages pyautogui)")
        return self._gui

    def size(self) -> tuple[int, int]:
        return tuple(self._require().size())  # type: ignore[return-value]

    def screenshot(self, path: str) -> str:
        self._require().screenshot(path)
        return path

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self._require().click(x=x, y=y, button=button, clicks=clicks)

    def move(self, x: int, y: int) -> None:
        self._require().moveTo(x, y)

    def type_text(self, text: str) -> None:
        self._require().write(text, interval=0.01)

    def press(self, key: str, *, presses: int = 1) -> None:
        self._require().press(key, presses=presses)

    def scroll(self, amount: int) -> None:
        self._require().scroll(amount)

    def clipboard_read(self) -> str:
        return str(self._require().paste())

    def clipboard_write(self, text: str) -> None:
        self._require().copy(text)

    def raise_window(self, title: str) -> None:
        raise BackendUnavailable("pyautogui cannot raise windows; install xdotool on Linux")

    def launch(self, command: list[str]) -> int:
        return subprocess.Popen(command, start_new_session=True).pid  # noqa: S603 - explicit command list


class ShellScreenBackend(ScreenBackend):
    """Linux/X11 backend built on the tools that are usually already present.

    Uses ``xdotool`` for input and windows, ``import``/``scrot``/``gnome-screenshot`` for captures and
    ``xclip``/``xsel`` for the clipboard. Wayland sessions are reported as unavailable rather than
    miscontrolled.
    """

    name = "shell-x11"

    def __init__(self) -> None:
        self._tools = {tool: shutil.which(tool) for tool in
                       ("xdotool", "import", "scrot", "gnome-screenshot", "xclip", "xsel")}

    @property
    def available(self) -> bool:
        return bool(os.environ.get("DISPLAY")) and bool(self._tools.get("xdotool")) and \
            bool(self._tools.get("import") or self._tools.get("scrot") or self._tools.get("gnome-screenshot"))

    def _run(self, args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=30, check=False)

    def _require(self) -> None:
        if not self.available:
            raise BackendUnavailable(
                "no X11 automation backend (need $DISPLAY plus xdotool and a screenshot tool)")

    def size(self) -> tuple[int, int]:
        self._require()
        result = self._run([self._tools["xdotool"], "getdisplaygeometry"])
        width, height = result.stdout.split()
        return int(width), int(height)

    def screenshot(self, path: str) -> str:
        self._require()
        if self._tools.get("import"):
            self._run([self._tools["import"], "-window", "root", path])
        elif self._tools.get("scrot"):
            self._run([self._tools["scrot"], path])
        else:
            self._run([self._tools["gnome-screenshot"], "-f", path])
        if not os.path.exists(path):
            raise NatashaError("screenshot backend produced no file")
        return path

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self._require()
        self._run([self._tools["xdotool"], "mousemove", str(x), str(y)])
        buttons = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        self._run([self._tools["xdotool"], "click", "--repeat", str(max(1, clicks)), buttons])

    def move(self, x: int, y: int) -> None:
        self._require()
        self._run([self._tools["xdotool"], "mousemove", str(x), str(y)])

    def type_text(self, text: str) -> None:
        self._require()
        self._run([self._tools["xdotool"], "type", "--clearmodifiers", "--delay", "10", text])

    def press(self, key: str, *, presses: int = 1) -> None:
        self._require()
        self._run([self._tools["xdotool"], "key", "--repeat", str(max(1, presses)), key])

    def scroll(self, amount: int) -> None:
        self._require()
        button = "4" if amount > 0 else "5"
        self._run([self._tools["xdotool"], "click", "--repeat", str(max(1, abs(amount))), button])

    def clipboard_read(self) -> str:
        if not self._tools.get("xclip"):
            raise BackendUnavailable("xclip is not installed")
        return self._run([self._tools["xclip"], "-selection", "clipboard", "-o"]).stdout

    def clipboard_write(self, text: str) -> None:
        if not self._tools.get("xclip"):
            raise BackendUnavailable("xclip is not installed")
        self._run([self._tools["xclip"], "-selection", "clipboard"], stdin=text)

    def raise_window(self, title: str) -> None:
        self._require()
        result = self._run([self._tools["xdotool"], "search", "--name", title])
        window_ids = [line for line in result.stdout.split() if line.strip()]
        if not window_ids:
            raise NatashaError(f"no window matches {title!r}")
        self._run([self._tools["xdotool"], "windowactivate", "--sync", window_ids[-1]])

    def launch(self, command: list[str]) -> int:
        return subprocess.Popen(command, start_new_session=True).pid  # noqa: S603


class NullBackend(ScreenBackend):
    """The honest fallback: it explains what to install instead of doing nothing silently."""

    name = "unavailable"

    def __init__(self) -> None:
        self.reason = ("no desktop automation backend found: install pyautogui "
                       "(python3 -m pip install --break-system-packages pyautogui) or, on Linux/X11, "
                       "xdotool plus a screenshot tool")

    def _raise(self) -> None:
        raise ComputerUnavailable(self.reason)

    def size(self) -> tuple[int, int]:
        self._raise()
        raise AssertionError("unreachable")

    def screenshot(self, path: str) -> str:
        self._raise()
        raise AssertionError("unreachable")

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self._raise()

    def move(self, x: int, y: int) -> None:
        self._raise()

    def type_text(self, text: str) -> None:
        self._raise()

    def press(self, key: str, *, presses: int = 1) -> None:
        self._raise()

    def scroll(self, amount: int) -> None:
        self._raise()

    def clipboard_read(self) -> str:
        self._raise()
        raise AssertionError("unreachable")

    def clipboard_write(self, text: str) -> None:
        self._raise()

    def raise_window(self, title: str) -> None:
        self._raise()

    def launch(self, command: list[str]) -> int:
        self._raise()
        raise AssertionError("unreachable")


def detect_backend(preferred: str = "auto") -> ScreenBackend:
    """Pick the best available backend, or the null backend with a reason."""
    candidates: list[ScreenBackend] = [PyAutoGUIBackend(), ShellScreenBackend()]
    if preferred != "auto":
        for candidate in candidates:
            if candidate.name == preferred:
                if candidate.available:
                    return candidate
                raise BackendUnavailable(f"backend {preferred!r} is not available on this host")
        raise BackendUnavailable(f"unknown backend {preferred!r}")
    for candidate in candidates:
        if candidate.available:
            return candidate
    return NullBackend()


class ComputerController:
    """Gated desktop control: policy, approval, audit - then action."""

    def __init__(self, *, policy: Any = None, approvals: Any = None, log: Any = None,
                 artifacts: Any = None, backend: ScreenBackend | str | None = None,
                 workspace: Any = None, tools: Any = None) -> None:
        self.policy = policy
        self.approvals = approvals
        self.log = log or get_event_log()
        self.artifacts = artifacts
        self.workspace = workspace
        self.tools = tools
        if isinstance(backend, ScreenBackend):
            self.backend = backend
        else:
            self.backend = detect_backend(backend or "auto")
        self.actions: list[ComputerAction] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ gating
    def _guard(self, capability: Capability, *, resource: str, arguments: dict[str, Any],
               risk: RiskLevel | None = None, actor: str = "owner", approval_id: str = "") -> None:
        if self.policy is None:
            return
        decision = self.policy.check(PolicyRequest(capability=capability, actor=actor, resource=resource,
                                                   context=dict(arguments), risk_hint=risk))
        if decision.effect.value == "deny":
            raise AccessDenied(f"{capability.value} on {resource!r} denied: {decision.reason}")
        if decision.effect.value == "approval":
            if approval_id:
                # An approval id was supplied: the registry validates and consumes it, not this layer.
                return
            request_id = ""
            if self.approvals is not None:
                try:
                    request = self.approvals.request(operation=capability.value, arguments=arguments,
                                                     resource=resource, requested_by=actor,
                                                     risk=decision.risk)
                    request_id = getattr(request, "id", "") or str(request)
                except Exception as exc:
                    raise AccessDenied(f"could not open an approval request: {exc}") from exc
            raise ApprovalRequired(f"{capability.value} on {resource!r} needs owner approval",
                                   approval_request_id=request_id, risk=decision.risk.name)

    def _record(self, action: ComputerAction, capability: Capability, resource: str) -> None:
        with self._lock:
            self.actions.append(action)
            self.actions = self.actions[-500:]
        self.log.append(EventKind.TOOL, {"action": "computer", **action.to_dict(),
                                         "capability": capability.value, "resource": resource},
                        actor="owner", source="computer",
                        risk=RiskLevel.HIGH if not action.ok else RiskLevel.MEDIUM)

    # ------------------------------------------------------------------ capabilities
    def capabilities(self) -> dict[str, Any]:
        return {"backend": self.backend.name, "available": self.backend.available,
                "reason": getattr(self.backend, "reason", ""),
                "actions": {"screenshot": self.backend.available, "click": self.backend.available,
                            "type": self.backend.available, "clipboard": self.backend.available,
                            "windows": self.backend.name in ("pyautogui", "shell-x11")}}

    def screen_size(self) -> dict[str, Any]:
        width, height = self.backend.size()
        return {"width": width, "height": height, "backend": self.backend.name}

    def screenshot(self, *, name: str = "", actor: str = "owner") -> dict[str, Any]:
        action = ComputerAction(kind="screenshot", arguments={"name": name})
        resource = "screen:root"
        self._guard(Capability.SCREEN_CAPTURE, resource=resource, arguments=action.arguments, actor=actor)
        started = time.perf_counter()
        try:
            from ..core import get_paths

            target = (self.artifacts or get_paths().artifacts) / (name or f"screen-{action.id}.png")
            target.parent.mkdir(parents=True, exist_ok=True)
            self.backend.screenshot(str(target))
            action.ok, action.artifact = True, str(target)
        except Exception as exc:
            action.error = f"{type(exc).__name__}: {exc}"
        finally:
            action.duration_ms = (time.perf_counter() - started) * 1000
        self._record(action, Capability.SCREEN_CAPTURE, resource)
        return action.to_dict()

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1, actor: str = "owner",
              approval_id: str = "") -> dict[str, Any]:
        arguments = {"x": int(x), "y": int(y), "button": button, "clicks": int(clicks)}
        action = ComputerAction(kind="click", arguments=arguments)
        resource = f"screen:pointer@{x},{y}"
        self._guard(Capability.INPUT_CONTROL, resource=resource, arguments=arguments, actor=actor,
                    approval_id=approval_id)
        return self._perform(action, Capability.INPUT_CONTROL, resource,
                             lambda: self.backend.click(int(x), int(y), button=button, clicks=int(clicks)))

    def move(self, x: int, y: int, *, actor: str = "owner", approval_id: str = "") -> dict[str, Any]:
        arguments = {"x": int(x), "y": int(y)}
        action = ComputerAction(kind="move", arguments=arguments)
        resource = f"screen:pointer@{x},{y}"
        self._guard(Capability.INPUT_CONTROL, resource=resource, arguments=arguments, actor=actor,
                    approval_id=approval_id)
        return self._perform(action, Capability.INPUT_CONTROL, resource, lambda: self.backend.move(int(x), int(y)))

    def type_text(self, text: str, *, actor: str = "owner", approval_id: str = "") -> dict[str, Any]:
        action = ComputerAction(kind="type", arguments={"text": text})
        resource = "screen:keyboard"
        self._guard(Capability.INPUT_CONTROL, resource=resource, arguments=action.arguments, actor=actor,
                    approval_id=approval_id)
        # Typing may include secrets: the audit record keeps the length, never the text.
        return self._perform(action, Capability.INPUT_CONTROL, resource, lambda: self.backend.type_text(text))

    def press(self, key: str, *, presses: int = 1, actor: str = "owner", approval_id: str = "") -> dict[str, Any]:
        action = ComputerAction(kind="key", arguments={"key": key, "presses": int(presses)})
        resource = f"screen:key:{key}"
        self._guard(Capability.INPUT_CONTROL, resource=resource, arguments=action.arguments, actor=actor,
                    approval_id=approval_id)
        return self._perform(action, Capability.INPUT_CONTROL, resource,
                             lambda: self.backend.press(key, presses=int(presses)))

    def scroll(self, amount: int, *, actor: str = "owner", approval_id: str = "") -> dict[str, Any]:
        action = ComputerAction(kind="scroll", arguments={"amount": int(amount)})
        resource = "screen:pointer"
        self._guard(Capability.INPUT_CONTROL, resource=resource, arguments=action.arguments, actor=actor,
                    approval_id=approval_id)
        return self._perform(action, Capability.INPUT_CONTROL, resource, lambda: self.backend.scroll(int(amount)))

    def clipboard_read(self, *, actor: str = "owner") -> dict[str, Any]:
        action = ComputerAction(kind="clipboard_read", arguments={})
        resource = "system:clipboard"
        self._guard(Capability.CLIPBOARD_READ, resource=resource, arguments={}, actor=actor)
        started = time.perf_counter()
        try:
            text = self.backend.clipboard_read()
            action.ok = True
            action.arguments = {"text": text}
        except Exception as exc:
            action.error = f"{type(exc).__name__}: {exc}"
        finally:
            action.duration_ms = (time.perf_counter() - started) * 1000
        self._record(action, Capability.CLIPBOARD_READ, resource)
        record = action.to_dict()
        record["text"] = action.arguments.get("text", "") if action.ok else ""
        return record

    def clipboard_write(self, text: str, *, actor: str = "owner", approval_id: str = "") -> dict[str, Any]:
        action = ComputerAction(kind="clipboard_write", arguments={"text": text})
        resource = "system:clipboard"
        self._guard(Capability.CLIPBOARD_WRITE, resource=resource, arguments=action.arguments, actor=actor,
                    approval_id=approval_id)
        return self._perform(action, Capability.CLIPBOARD_WRITE, resource, lambda: self.backend.clipboard_write(text))

    def raise_window(self, title: str, *, actor: str = "owner", approval_id: str = "") -> dict[str, Any]:
        action = ComputerAction(kind="raise_window", arguments={"title": title})
        resource = f"window:{title}"
        self._guard(Capability.WINDOW_CONTROL, resource=resource, arguments=action.arguments, actor=actor,
                    approval_id=approval_id)
        return self._perform(action, Capability.WINDOW_CONTROL, resource, lambda: self.backend.raise_window(title))

    def launch(self, command: str | list[str], *, actor: str = "owner", approval_id: str = "") -> dict[str, Any]:
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        action = ComputerAction(kind="launch", arguments={"command": argv})
        resource = f"app:{argv[0] if argv else '?'}"
        self._guard(Capability.APP_CONTROL, resource=resource, arguments={"command": argv},
                    risk=RiskLevel.HIGH, actor=actor, approval_id=approval_id)
        started = time.perf_counter()
        try:
            pid = self.backend.launch(argv)
            action.ok = True
            action.arguments = {"command": argv, "pid": pid}
        except Exception as exc:
            action.error = f"{type(exc).__name__}: {exc}"
        finally:
            action.duration_ms = (time.perf_counter() - started) * 1000
        self._record(action, Capability.APP_CONTROL, resource)
        return action.to_dict()

    def _perform(self, action: ComputerAction, capability: Capability, resource: str, call: Any) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            call()
            action.ok = True
        except Exception as exc:
            action.error = f"{type(exc).__name__}: {exc}"
        finally:
            action.duration_ms = (time.perf_counter() - started) * 1000
        self._record(action, capability, resource)
        return action.to_dict()

    def history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [action.to_dict() for action in self.actions[-limit:]]


_CONTROLLER: ComputerController | None = None
_LOCK = threading.Lock()


def get_computer_controller(**kwargs: Any) -> ComputerController:
    global _CONTROLLER
    with _LOCK:
        if _CONTROLLER is None:
            _CONTROLLER = ComputerController(**kwargs)
        return _CONTROLLER


def reset_computer_controller() -> None:
    global _CONTROLLER
    with _LOCK:
        _CONTROLLER = None
