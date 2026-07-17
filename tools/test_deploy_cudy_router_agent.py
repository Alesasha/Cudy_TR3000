#!/usr/bin/env python3
"""Regression checks for the safe Cudy router-agent deployment defaults."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from deploy_cudy_router_agent import build_parser, validate_artifact_freshness


def main() -> int:
    parser = build_parser()
    defaults = parser.parse_args([])
    assert defaults.timeout >= 120, "deploy timeout must cover the first observe health cycle"
    assert not defaults.enable_apply, "apply must never be enabled by default"
    assert not defaults.disable_apply

    observe = parser.parse_args(["--disable-apply", "--dry-run"])
    assert observe.disable_apply
    assert not observe.enable_apply

    apply = parser.parse_args(["--enable-apply", "--dry-run"])
    assert apply.enable_apply
    assert not apply.disable_apply
    init = (ROOT / "openwrt" / "cudy-router-agent.init").read_text(encoding="utf-8")
    assert "-authoritative-overrides" in init

    temp = ROOT / "build" / "test-router-agent-freshness"
    source = ROOT / "build" / "test-router-agent-source.go"
    try:
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(b"binary")
        source.write_text("package main\n", encoding="utf-8")
        source.touch()
        try:
            validate_artifact_freshness(temp, (source,))
        except RuntimeError as exc:
            assert "Build-CudyRouterAgentGo.ps1" in str(exc)
        else:
            raise AssertionError("stale router-agent artifact was accepted")
    finally:
        temp.unlink(missing_ok=True)
        source.unlink(missing_ok=True)
    print("Cudy router-agent deploy defaults regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
