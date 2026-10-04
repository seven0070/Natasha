"""Helpers for calling async code from sync contexts without breaking a running event loop.

A *single* background loop is kept alive for the process. That matters beyond convenience: async
resources that are cached across calls - MCP sessions, browser handles, provider clients - are bound
to the loop that created them, so ``asyncio.run`` per call would leave a live session attached to a
closed loop and every later request would hang until it timed out.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine

_LOOP: asyncio.AbstractEventLoop | None = None
_THREAD: threading.Thread | None = None
_LOCK = threading.Lock()


def background_loop() -> asyncio.AbstractEventLoop:
    """The process-wide loop used by :func:`run_coroutine_sync`."""
    global _LOOP, _THREAD
    with _LOCK:
        if _LOOP is None or _LOOP.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="natasha-async-loop", daemon=True)
            thread.start()
            _LOOP, _THREAD = loop, thread
        return _LOOP


def run_coroutine_sync(coro: Coroutine[Any, Any, Any], *, timeout: float | None = None) -> Any:
    """Run *coro* to completion from synchronous code.

    Works whether or not the calling thread already has a running loop (FastAPI handlers, async
    tests, plain scripts): the coroutine is executed on the shared background loop, so a cached
    session created by one call is still valid for the next one.
    """
    loop = background_loop()
    future = asyncio.run_coroutine_threadsafe(
        asyncio.wait_for(coro, timeout) if timeout else coro, loop
    )
    try:
        return future.result(timeout + 10 if timeout else None)
    except BaseException:
        future.cancel()
        raise


def current_loop_id() -> int:
    """Identity of the running loop, or 0 when called from a thread without one."""
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def reset_async_bridge() -> None:
    """Stop the background loop (used by tests between homes)."""
    global _LOOP, _THREAD
    with _LOCK:
        loop, thread = _LOOP, _THREAD
        _LOOP, _THREAD = None, None
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None:
        thread.join(timeout=5)
    if loop is not None and not loop.is_closed():
        loop.close()
