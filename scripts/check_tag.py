#!/usr/bin/env python3
"""Exit non-zero unless the given tag (vX.Y.Z) matches manifest.json's version."""

from __future__ import annotations

import json
import sys
from pathlib import Path

MANIFEST = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "aosmith_ble"
    / "manifest.json"
)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check_tag.py vX.Y.Z", file=sys.stderr)
        return 2
    tag = argv[0]
    if not tag.startswith("v") or len(tag) == 1:
        print(f"tag {tag!r} must look like vX.Y.Z", file=sys.stderr)
        return 1
    version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
    if tag[1:] != version:
        print(
            f"tag {tag!r} does not match manifest version {version!r}", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
