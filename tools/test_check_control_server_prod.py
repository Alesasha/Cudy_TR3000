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
    assert not defaults.via_cudy
    private = build_parser().parse_args(["--via-cudy"])
    assert private.via_cudy
    assert private.cudy_host == "192.168.8.1"
    assert private.private_host == "172.29.172.1"
    assert private.private_port == 22
    assert not private.no_direct_ssh_fallback
    smoke_source = (TOOLS / "vpn_smoke_check.py").read_text(encoding="utf-8")
    assert '"--via-cudy"' in smoke_source
    assert '"60"' in smoke_source
    print("Control-server production audit defaults regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
