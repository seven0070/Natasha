"""``python -m natasha.cli`` — the same entry point as the ``natasha`` console script.

Kept as a module rather than documented as ``python -m natasha.cli.main`` because running a
submodule of a package that imports it produces a runpy warning ("found in sys.modules after import
of package ..."), which looks like a fault on the first command a new user runs.
"""

from __future__ import annotations

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
