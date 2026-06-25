#!/usr/bin/env python3
"""Check probe failure warning windows in production system status."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vpn_control_app import PROBE_FAILED_WARN_SECONDS, build_system_status, init_db


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
    print("System status probe warning smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
