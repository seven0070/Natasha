"""Reaping child processes.

Every place that spawns a subprocess has to answer one question when it is done: is the child really
gone? ``kill()`` only *sends* a signal; a `Process` object that is never awaited keeps the child's
pipe transports open, and a cancelled ``communicate()`` leaves the child running. The result is a
"timed out" tool that is still executing in the background and a transport that is garbage collected
after the event loop closed (an unraisable ``RuntimeError``).

``terminate`` is the single answer: signal, await, escalate, close the pipes. It never raises, so it
is safe to call from an ``except`` or a ``finally``.
"""

from __future__ import annotations

import asyncio
from typing import Any

#: How long to wait for a polite SIGTERM before escalating to SIGKILL.
GRACE_SECONDS = 5.0


async def terminate(process: Any, *, grace: float = GRACE_SECONDS) -> None:
    """Stop *process* and reap it, closing its pipes. Safe to call in a ``finally``.

    Escalation is deliberate: a process that ignores SIGTERM must not be allowed to outlive the
    call that started it, because the caller has already reported the operation as finished.
    """
    if process is None:
        return
    try:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=grace)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=grace)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass
    except Exception:  # pragma: no cover - cleanup must never mask the original error
        pass
    finally:
        _close_pipes(process)


def _close_pipes(process: Any) -> None:
    for stream in (getattr(process, "stdin", None), getattr(process, "stdout", None),
                   getattr(process, "stderr", None)):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:
            continue


def is_running(process: Any) -> bool:
    """True when the child has not exited yet (``returncode is None``)."""
    return process is not None and process.returncode is None


__all__ = ["GRACE_SECONDS", "is_running", "terminate"]
