"""The production server entry point: ``python -m apps.server [--host H] [--port P]``.

This exists so a container, a systemd unit or a process supervisor has a stable module to run that
does not depend on the console script being on ``PATH``. It performs no work of its own beyond
defaulting the bind address from settings and delegating to ``natasha serve`` - the same code path
the CLI and the container use, so there is one server implementation, not three.

Environment:

``NATASHA_HOME``      where the agent keeps its data (default ``~/.natasha``)
``NATASHA_CONFIG``    path to a settings file to layer on top of the checkout's ``config/``
``NATASHA_HOST``      bind address when ``--host`` is not given (containers set ``0.0.0.0``)
``NATASHA_PORT``      port when ``--port`` is not given
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Run the API + web console. Returns a process exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "-h" in arguments or "--help" in arguments:
        print(__doc__)
        return 0
    from natasha.cli.main import main as cli_main

    return cli_main(["serve", *arguments])


if __name__ == "__main__":  # pragma: no cover - exercised by the container smoke test
    raise SystemExit(main())
