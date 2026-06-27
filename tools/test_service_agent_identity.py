#!/usr/bin/env python3
"""Regression check for non-login service users and agent device tokens."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "tools" / "vpn_control_app.py"


def run(db: Path, *args: str) -> str:
    command = [sys.executable, str(APP), "--db", str(db), *args]
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(command)}\n{result.stdout}")
    return result.stdout


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cudy-service-agent-") as tmp:
        db = Path(tmp) / "vpn_control.db"
        user_raw = run(
            db,
            "service-user-create",
            "cudy_lan",
            "--display-name",
            "Cudy LAN Agent",
            "--json",
        )
        user = json.loads(user_raw)
        if user["id"] != "cudy_lan" or user["role"] != "user":
            raise AssertionError(f"unexpected service user: {user}")

        device_raw = run(
            db,
            "device-create",
            "cudy_lan",
            "--device-id",
            "cudy-home",
            "--display-name",
            "Cudy Home Router",
            "--platform",
            "other",
            "--json",
        )
        device = json.loads(device_raw)
        if device["id"] != "cudy-home" or device["user_id"] != "cudy_lan":
            raise AssertionError(f"unexpected device: {device}")
        if not str(device["token"]).startswith("vca_"):
            raise AssertionError("device token prefix mismatch")

        devices = json.loads(run(db, "device-list", "--json"))
        if len(devices) != 1 or devices[0]["id"] != "cudy-home":
            raise AssertionError(f"unexpected device list: {devices}")

    print("Service agent identity regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
