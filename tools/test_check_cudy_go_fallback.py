#!/usr/bin/env python3
"""Regression checks for Cudy fallback audit timeout contracts."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from check_cudy_go_fallback import build_parser  # noqa: E402


def main() -> int:
    defaults = build_parser().parse_args([])
    source = (TOOLS / "check_cudy_go_fallback.py").read_text(encoding="utf-8")
    assert defaults.timeout > 25, "SSH command timeout must exceed the preview request timeout"
    assert "curl -fsS --max-time 25 http://127.0.0.1:8765/api/cudy/agent-preview" in source
    print("Cudy Go fallback audit timeout regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
