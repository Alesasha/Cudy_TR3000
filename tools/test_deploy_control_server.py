#!/usr/bin/env python3
"""Regression checks for control-server deployment payload selection."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import deploy_control_server as deploy


def main() -> int:
    code_only = deploy.selected_upload_dirs(include_agent_updates=False)
    with_updates = deploy.selected_upload_dirs(include_agent_updates=True)
    assert deploy.AGENT_UPDATE_DIR not in code_only
    assert deploy.AGENT_UPDATE_DIR in with_updates
    assert set(deploy.UPLOAD_DIRS).issubset(code_only)

    defaults = deploy.build_parser().parse_args([])
    assert not defaults.skip_agent_updates
    selected = deploy.build_parser().parse_args(["--skip-agent-updates"])
    assert selected.skip_agent_updates
    print("Control-server deploy payload regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
