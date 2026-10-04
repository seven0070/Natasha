"""Fixture skill: reads a JSON payload on stdin, writes a JSON result on stdout."""
import json
import sys


def main() -> None:
    payload = json.load(sys.stdin)
    text = str(payload.get("text", ""))
    json.dump({"ok": True, "output": {"words": len(text.split()), "characters": len(text)}}, sys.stdout)


if __name__ == "__main__":
    main()
