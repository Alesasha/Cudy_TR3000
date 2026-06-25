#!/usr/bin/env python3
"""Check that missing LokVPN subscription profiles are disabled, not left stale."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from vpn_control_app import refresh_lokvpn_transports


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "config" / "vpn_inventory.json"


def main() -> int:
    fake_subscription = {
        "outbounds": [
            {
                "protocol": "vless",
                "tag": "DE",
                "settings": {"vnext": []},
            }
        ]
    }
    with tempfile.TemporaryDirectory(prefix="cudy-lokvpn-refresh-", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "vpn_control.db"
        result = refresh_lokvpn_transports(
            db_path,
            INVENTORY,
            server_ids=["lokvpn-nl1"],
            subscription=fake_subscription,
        )
        failed = result.get("failed") or []
        if len(failed) != 1 or failed[0].get("server_id") != "lokvpn-nl1":
            raise AssertionError(f"unexpected refresh result: {result!r}")
        if failed[0].get("disabled") is not True:
            raise AssertionError(f"missing profile did not mark transport disabled: {result!r}")
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT enabled, config_json, source, version FROM transport_configs WHERE server_id = ?",
                ("lokvpn-nl1",),
            ).fetchone()
        if row is None:
            raise AssertionError("disabled transport config was not saved")
        enabled, config_json, source, version = row
        config = json.loads(config_json)
        if enabled != 0 or config.get("unavailable") is not True:
            raise AssertionError(f"transport config was not disabled: enabled={enabled} config={config!r}")
        if source != "lokvpn-subscription" or version != "nl1":
            raise AssertionError(f"unexpected transport metadata: source={source!r} version={version!r}")
    print("LokVPN unavailable refresh smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
