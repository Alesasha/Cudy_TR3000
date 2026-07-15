#!/usr/bin/env python3
"""Regression checks for production control-server audit defaults."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from check_control_server_prod import build_parser


def main() -> int:
    defaults = build_parser().parse_args([])
    assert defaults.timeout >= 60, "audit timeout must cover remote system-status checks"
    assert defaults.connect_attempts >= 3
    assert defaults.http_fallback_url
    assert not defaults.require_ssh
    print("Control-server production audit defaults regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
