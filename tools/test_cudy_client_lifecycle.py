#!/usr/bin/env python3
"""Local regression checks for Cudy client import/delete lifecycle."""

from __future__ import annotations

import sys
import tempfile
import gc
from contextlib import closing
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import vpn_control_app as app  # noqa: E402


INVENTORY = ROOT / "config" / "vpn_inventory.json"


def assert_equal(actual: Any, expected: Any, context: str) -> None:
    if actual != expected:
        raise AssertionError(f"{context}: expected {expected!r}, got {actual!r}")


def write_conf(path: Path, *, address: str) -> None:
    path.write_text(
        "\n".join(
            [
                "[Interface]",
                "PrivateKey = test",
                f"Address = {address}/32",
                "",
                "[Peer]",
                "PublicKey = test",
                "AllowedIPs = 0.0.0.0/0",
                "",
            ]
        ),
        encoding="ascii",
        newline="\n",
    )


def user_row(db_path: Path, user_id: str) -> dict[str, Any] | None:
    with closing(app.connect(db_path)) as conn:
        return app.row(conn, "SELECT id, role, client_ip, enabled FROM users WHERE id = ?", (user_id,))


def run_import_duplicate_address_check(db_path: Path, source_dir: Path) -> None:
    write_conf(source_dir / "alice-awg.conf", address="10.77.0.20")
    write_conf(source_dir / "duplicate-awg.conf", address="10.77.0.20")
    write_conf(source_dir / "bob-awg.conf", address="10.77.0.21")

    imported = app.import_cudy_clients(db_path, INVENTORY, source_dir)
    assert_equal([item["id"] for item in imported], ["alice", "bob"], "duplicate client IP should be skipped")
    assert_equal(user_row(db_path, "alice")["client_ip"], "10.77.0.20", "alice client ip")
    assert_equal(user_row(db_path, "bob")["client_ip"], "10.77.0.21", "bob client ip")
    assert_equal(user_row(db_path, "duplicate"), None, "duplicate user should not be created")


def run_delete_local_config_check(db_path: Path, output_dir: Path) -> None:
    original_output_dir = app.CUDY_CLIENT_OUTPUT_DIR
    try:
        app.CUDY_CLIENT_OUTPUT_DIR = output_dir
        app.create_or_update_user(
            db_path,
            INVENTORY,
            user_id="delete-me",
            display_name="Delete Me",
            role="user",
            password=None,
            client_ip="10.77.0.30",
            enabled=True,
            allow_no_password=True,
        )
        config_path = app.cudy_client_config_path("delete-me")
        write_conf(config_path, address="10.77.0.30")

        result = app.delete_admin_user(db_path, INVENTORY, user_id="delete-me", revoke_cudy=False)
        assert_equal(result["ok"], True, "delete result")
        assert_equal(result["revoke_cudy"], False, "local delete should not revoke remote peer")
        assert_equal(config_path.exists(), False, "local config should be removed")
        assert_equal(user_row(db_path, "delete-me"), None, "user should be removed")
    finally:
        app.CUDY_CLIENT_OUTPUT_DIR = original_output_dir


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cudy-client-lifecycle-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        db_path = root / "vpn_control.db"
        source_dir = root / "source"
        output_dir = root / "configs"
        source_dir.mkdir()
        output_dir.mkdir()
        run_import_duplicate_address_check(db_path, source_dir)
        run_delete_local_config_check(db_path, output_dir)
        gc.collect()
    print("Cudy client lifecycle regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
