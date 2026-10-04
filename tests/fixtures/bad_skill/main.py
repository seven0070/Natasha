"""Fixture skill that deliberately contains scan-tripping patterns (never executed)."""

import os
import subprocess  # noqa: F401 - fixture only

TOKEN = os.environ.get("NATASHA_MASTER_KEY")


def main() -> None:
    subprocess.run("echo pwned", shell=True)  # noqa: S602 - fixture only
    eval("1 + 1")


if __name__ == "__main__":
    main()
