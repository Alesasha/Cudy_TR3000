#!/usr/bin/env python3
"""Check probe failure warning windows in production system status."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import vpn_control_app
from vpn_control_app import (
    PROBE_FAILED_WARN_SECONDS,
    build_readiness_status,
    build_system_status,
    init_db,
    save_transport_config,
)


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "config" / "vpn_inventory.json"


def insert_failed_probe(conn: sqlite3.Connection, *, job_id: str, timestamp: str) -> None:
    conn.execute(
        """
        INSERT INTO agent_probe_jobs (
          id, domain, user_id, candidate_server_ids, url, status,
          assigned_device_id, claimed_by_device_id, apply_cache,
          connect_timeout, max_time, priority, attempts,
          result_json, created_at, updated_at, started_at, finished_at
        ) VALUES (?, 'example.com', '', '[]', NULL, 'failed',
                  '', '', 1, 5, 12, 100, 1, '{}', ?, ?, ?, ?)
        """,
        (job_id, timestamp, timestamp, timestamp, timestamp),
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cudy-status-probes-", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "vpn_control.db"
        init_db(db_path, INVENTORY)
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=PROBE_FAILED_WARN_SECONDS + 120)).replace(microsecond=0).isoformat()
        recent_time = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with sqlite3.connect(db_path) as conn:
            insert_failed_probe(conn, job_id="old-failed", timestamp=old_time)
            conn.commit()
        old_status = build_system_status(db_path, INVENTORY)
        if old_status["probe_jobs"]["failed"] != 1 or old_status["probe_jobs"]["failed_recent"] != 0:
            raise AssertionError(f"old failed job counters are wrong: {old_status['probe_jobs']!r}")
        if any("probe job" in warning for warning in old_status["warnings"]):
            raise AssertionError(f"old failed job should not warn: {old_status['warnings']!r}")
        with sqlite3.connect(db_path) as conn:
            insert_failed_probe(conn, job_id="recent-failed", timestamp=recent_time)
            conn.commit()
        recent_status = build_system_status(db_path, INVENTORY)
        if recent_status["probe_jobs"]["failed_recent"] != 1:
            raise AssertionError(f"recent failed job counter is wrong: {recent_status['probe_jobs']!r}")
        if not any("probe job" in warning for warning in recent_status["warnings"]):
            raise AssertionError(f"recent failed job should warn: {recent_status['warnings']!r}")
        readiness = build_readiness_status(db_path, INVENTORY)
        if readiness["ok"] is not True:
            raise AssertionError(f"recent failed probe should not make readiness fail: {readiness!r}")
        probe_check = next((item for item in readiness["checks"] if item.get("name") == "probe_jobs"), {})
        if probe_check.get("state") != "warn" or probe_check.get("ok") is not True:
            raise AssertionError(f"probe readiness check should be warning-only: {probe_check!r}")

        original_fallback_status = vpn_control_app.cudy_fallback_state_status
        try:
            vpn_control_app.cudy_fallback_state_status = lambda: (_ for _ in ()).throw(
                AssertionError("readiness must not perform external fallback I/O")
            )
            build_readiness_status(db_path, INVENTORY)
        finally:
            vpn_control_app.cudy_fallback_state_status = original_fallback_status

        save_transport_config(
            db_path,
            INVENTORY,
            server_id="proxyde",
            transport_type="http-proxy-tun",
            interface_name="proxyde",
            config={"server": "127.0.0.1", "server_port": 8080},
            enabled=True,
            source="test",
        )
        stale_time = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE transport_configs SET updated_at = ? WHERE server_id = 'proxyde'", (stale_time,))
            conn.execute("UPDATE servers SET enabled = 0 WHERE id = 'proxyde'")
            conn.commit()
        disabled_status = build_system_status(db_path, INVENTORY)
        if disabled_status["transports"]["stale_enabled_count"] != 0:
            raise AssertionError(f"disabled server transport must not be stale-active: {disabled_status['transports']!r}")
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE servers SET enabled = 1 WHERE id = 'proxyde'")
            conn.commit()
        enabled_status = build_system_status(db_path, INVENTORY)
        if enabled_status["transports"]["stale_enabled_count"] != 1:
            raise AssertionError(f"enabled server transport must be stale-active: {enabled_status['transports']!r}")
        if build_readiness_status(db_path, INVENTORY)["ok"] is not False:
            raise AssertionError("stale active transport must fail readiness")

        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE servers SET enabled = 0 WHERE id = 'proxyde'")
            conn.commit()
        save_transport_config(
            db_path,
            INVENTORY,
            server_id="aktau",
            transport_type="amneziawg-conf",
            interface_name="AmneziaVPN",
            config={"endpoint": "198.51.100.10:51820"},
            enabled=True,
            source="test-static",
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE transport_configs SET updated_at = ? WHERE server_id = 'aktau'", (stale_time,))
            conn.commit()
        static_status = build_system_status(db_path, INVENTORY)
        if static_status["transports"]["stale_enabled_count"] != 0:
            raise AssertionError(f"static own AWG transport must not expire: {static_status['transports']!r}")
        if build_readiness_status(db_path, INVENTORY)["ok"] is not True:
            raise AssertionError("static own AWG transport age must not fail readiness")
    print("System status probe warning smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
