#!/usr/bin/env python3
"""Regression checks for global/user critical service policy."""

from __future__ import annotations

import tempfile
from pathlib import Path

import vpn_control_app as app


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "config" / "vpn_inventory.json"
USER_ID = "critical-test-user"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cudy-critical-services-", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "vpn_control.db"
        app.init_db(db_path, INVENTORY)
        timestamp = app.now()
        with app.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO users (id, display_name, role, default_server_id, enabled, created_at, updated_at)
                VALUES (?, 'Critical Test', 'user', 'auto', 1, ?, ?)
                """,
                (USER_ID, timestamp, timestamp),
            )

        app.save_critical_service(
            db_path,
            INVENTORY,
            user_id="",
            service_key="chatgpt",
            label="ChatGPT",
            targets="chatgpt.com, https://chatgpt.com/backend-api/codex/responses",
            failure_pattern=r"unsupported\s+country|access\s+denied",
        )
        with app.connect(db_path) as conn:
            effective = app.effective_critical_services(conn, user_id=USER_ID)
        chatgpt = next(item for item in effective if item["service_key"] == "chatgpt")
        assert chatgpt["targets"][0] == "https://chatgpt.com/"
        assert chatgpt["scope"] == "global"

        app.save_critical_service(
            db_path,
            INVENTORY,
            user_id=USER_ID,
            service_key="chatgpt",
            label="ChatGPT",
            targets="https://chatgpt.com/",
            enabled=False,
        )
        with app.connect(db_path) as conn:
            assert all(
                item["service_key"] != "chatgpt"
                for item in app.effective_critical_services(conn, user_id=USER_ID)
            )

        app.save_critical_service(
            db_path,
            INVENTORY,
            user_id=USER_ID,
            service_key="work",
            label="Work portal",
            targets="https://example.com/health",
            success_pattern=r"status\s*:\s*ok",
        )
        with app.connect(db_path) as conn:
            effective = app.effective_critical_services(conn, user_id=USER_ID)
            config = app.build_agent_config(
                conn,
                user_id=USER_ID,
                device={"id": "critical-test-device", "display_name": "Test", "platform": "windows"},
            )
            patterns = app.critical_probe_patterns(
                conn,
                user_id=USER_ID,
                domain="example.com",
                url="https://example.com/health",
            )
        assert "work" in [item["service_key"] for item in effective]
        work = next(item for item in config["critical_services"] if item["service_key"] == "work")
        assert work["success_pattern"] == r"status\s*:\s*ok"
        assert patterns == {"success_pattern": r"status\s*:\s*ok", "failure_pattern": ""}
        with app.connect(db_path) as conn:
            cidr_patterns = app.critical_probe_patterns(
                conn,
                user_id=USER_ID,
                domain="ip-149-154-160-0-20.iproute.local",
                url="tcp://149.154.167.51:443",
            )
        assert cidr_patterns == {"success_pattern": "", "failure_pattern": ""}

        try:
            app.save_critical_service(
                db_path,
                INVENTORY,
                user_id="",
                service_key="broken",
                label="Broken",
                targets="example.com",
                failure_pattern="(",
            )
        except ValueError as exc:
            assert "Invalid failure pattern" in str(exc)
        else:
            raise AssertionError("invalid regex must be rejected")

    print("Critical service policy regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
