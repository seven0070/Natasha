"""Fixture skill used by Natasha's skill-runtime tests: counts words in stdin JSON."""

import json
import sys


def main() -> None:
    payload = json.load(sys.stdin)
    text = str(payload.get("text", ""))
    print(json.dumps({
        "ok": True,
        "output": {
            "words": len(text.split()),
            "characters": len(text),
            "lines": len(text.splitlines()) or (1 if text else 0),
        },
    }))


if __name__ == "__main__":
    main()
