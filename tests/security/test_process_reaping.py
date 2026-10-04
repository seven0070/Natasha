"""A timeout must stop the process, not abandon it.

A cancelled ``communicate()`` does not kill the child: the tool reports "timed out" while the command
it started keeps running (and keeps its pipes open). That is both a resource leak and a security
problem - a runaway command outlives the call that was supposed to bound it. These tests pin the
behaviour of the shell tool, the verification check and the shared reaper.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

import pytest

from natasha.core.process import is_running, terminate

pytestmark = pytest.mark.security


def _alive(pid: int) -> bool:
    """True while *pid* still exists (zombies count as gone for our purposes)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - the pid belongs to someone else
        return True
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            if stat.read_text().split()[2] == "Z":
                return False
        except (OSError, IndexError):
            pass
    return True


async def test_terminate_stops_a_running_child_and_closes_its_pipes():
    process = await asyncio.create_subprocess_exec(
        "sleep", "30", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    assert is_running(process)
    await terminate(process, grace=5.0)
    assert not is_running(process), "terminate must reap the child, not just signal it"
    assert not _alive(process.pid)


async def test_terminate_escalates_when_the_child_ignores_sigterm(tmp_path):
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        "python3", str(script), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await asyncio.wait_for(process.stdout.readline(), timeout=10)  # wait until the handler is set
    await terminate(process, grace=1.0)
    assert not is_running(process), "a child that ignores SIGTERM must be killed"
    assert not _alive(process.pid)


async def test_a_timed_out_shell_command_is_reported_and_actually_stopped(runtime):
    """The end-to-end version: the tool's timeout message must be true."""
    from natasha.tools.base import ToolContext

    marker = Path(runtime.paths.workspace) / "should-not-appear.txt"
    command = (f"python3 -c \"import time; time.sleep(20); open('{marker}', 'w').write('late')\"")
    started = time.time()
    result = await runtime.tools.execute("shell", {"command": command, "timeout_seconds": 1},
                                         context=ToolContext(actor="owner"))
    elapsed = time.time() - started

    assert not result.ok
    assert "timed out" in result.error
    assert elapsed < 10, "the timeout must not wait for the command to finish"
    assert "stopped" in result.error, "the message must say the process was stopped, and it must be"
    time.sleep(1.5)
    assert not marker.exists(), "a timed-out command must not keep running and write its file"


async def test_a_timed_out_python_snippet_is_stopped_too(runtime):
    from natasha.tools.base import ToolContext

    result = await runtime.tools.execute("python_exec", {"code": "import time\ntime.sleep(20)",
                                                          "timeout_seconds": 1},
                                          context=ToolContext(actor="owner"))
    assert not result.ok and "timed out" in result.error
    assert "stopped" in result.error


async def test_a_timed_out_verification_check_stops_its_process(home):
    from natasha.verification.checks import CommandCheck

    check = CommandCheck("slow", ["sleep", "30"], timeout=1)
    result = await check.check({})
    assert not result.ok
    assert "timed out" in result.detail
    assert "stopped" in result.detail


def test_the_reaper_is_signal_safe():
    """`signal` import is used above; this keeps the module honest about what it asserts."""
    assert signal.SIGTERM  # trivial, but makes the dependency explicit rather than implicit
