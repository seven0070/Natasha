"""Fixture skill that would need powerful permissions (never executed in tests)."""
import json
import sys


def main() -> None:
    json.dump({"ok": True, "output": {"note": "this skill declares dangerous permissions"}}, sys.stdout)


if __name__ == "__main__":
    main()
