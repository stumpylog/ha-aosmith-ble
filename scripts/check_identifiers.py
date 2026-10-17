#!/usr/bin/env python3
"""Fail if a text file holds an em dash, a real-looking MAC or a real-looking name.

With file arguments, checks those files (the pre-commit hook passes the staged
ones). With none, checks every file git tracks or would track. The only allowed
identifiers are the placeholders: AA:BB:CC:DD:EE:FF and iCOMM-AC000W037XXXXXX.
The glob iCOMM-* is allowed because it has fewer than 10 characters after the dash.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

EM_DASH = chr(0x2014)
MAC_RE = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])"
)
NAME_RE = re.compile(r"iCOMM-[A-Za-z0-9]{10,}")
ALLOWED_MAC = "AA:BB:CC:DD:EE:FF"
ALLOWED_NAME = "iCOMM-AC000W037XXXXXX"


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [Path(p) for p in out.split("\0") if p and Path(p).is_file()]


def check_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError, OSError:
        return []  # binary or unreadable: not a text file
    problems: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if EM_DASH in line:
            problems.append(f"{path}:{lineno}: em dash")
        problems.extend(
            f"{path}:{lineno}: MAC-shaped string {m.group(0)!r}"
            for m in MAC_RE.finditer(line)
            if m.group(0) != ALLOWED_MAC
        )
        problems.extend(
            f"{path}:{lineno}: device name {m.group(0)!r}"
            for m in NAME_RE.finditer(line)
            if m.group(0) != ALLOWED_NAME
        )
    return problems


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] if argv else tracked_files()
    problems = [p for path in paths for p in check_file(path)]
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
