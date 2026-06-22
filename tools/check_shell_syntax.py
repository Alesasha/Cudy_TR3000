#!/usr/bin/env python3
"""Parse-check shell scripts when bash is available."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRS = [ROOT / "tools" / "agent-linux"]


def main() -> int:
    bash = shutil.which("bash")
    if not bash:
        print("bash not found; shell syntax check skipped.")
        return 0
    probe = subprocess.run(
        [bash, "--version"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if probe.returncode:
        first_line = (probe.stdout or "").splitlines()[0] if probe.stdout else "unknown error"
        print(f"bash is not usable in this environment; shell syntax check skipped: {first_line}")
        return 0
    scripts: list[Path] = []
    for directory in SCRIPT_DIRS:
        scripts.extend(sorted(directory.glob("*.sh")))
    failures: list[str] = []
    for script in scripts:
        result = subprocess.run(
            [bash, "-n", str(script)],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.returncode:
            failures.append(f"{script.relative_to(ROOT)}\n{result.stdout.strip()}")
    if failures:
        print("\n\n".join(failures))
        return 1
    print(f"Shell syntax ok: {len(scripts)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
