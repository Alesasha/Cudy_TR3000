#!/usr/bin/env python3
"""Regression checks for the OpenWrt control-tunnel installer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from install_cudy_control_tunnel import render_env  # noqa: E402


def test_rendered_environment_uses_lf_and_shell_quotes() -> None:
    args = argparse.Namespace(
        control_host="95.182.91.203",
        control_port=22,
        control_user="cudy-tunnel-cudy",
        local_port=18765,
        remote_host="127.0.0.1",
        remote_port=8765,
        remote_dir="/etc/cudy-fallback",
    )
    payload = render_env(args).encode("utf-8")
    assert b"\r" not in payload
    assert b"CONTROL_USER='cudy-tunnel-cudy'\n" in payload
    assert payload.endswith(b"\n")
