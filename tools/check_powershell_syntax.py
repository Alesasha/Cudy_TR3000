#!/usr/bin/env python3
"""Check PowerShell script parseability without executing the scripts."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATTERNS = [
    "tools/*.ps1",
    "tools/agent-windows/*.ps1",
]


def powershell_command(path: Path) -> list[str]:
    script = (
        "$ErrorActionPreference='Stop'; "
        f"$null = [scriptblock]::Create((Get-Content -Raw -LiteralPath {path.as_posix()!r})); "
        "Write-Output ok"
    )
    return ["powershell", "-NoProfile", "-Command", script]


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse-check PowerShell scripts.")
    parser.add_argument("paths", nargs="*", help="Explicit .ps1 files to check. Defaults to tools/*.ps1 and tools/agent-windows/*.ps1.")
    args = parser.parse_args()

    if args.paths:
        paths = [Path(item) for item in args.paths]
    else:
        found: list[Path] = []
        for pattern in DEFAULT_PATTERNS:
            found.extend(ROOT.glob(pattern))
        paths = sorted({path.resolve() for path in found if path.is_file()})

    failures: list[str] = []
    for path in paths:
        resolved = path if path.is_absolute() else ROOT / path
        result = subprocess.run(
            powershell_command(resolved),
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            failures.append(f"{resolved}: {result.stdout.strip()}")

    if failures:
        print("PowerShell syntax check failed:")
        for failure in failures:
            print(failure)
        return 1

    print(f"PowerShell syntax ok: {len(paths)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
