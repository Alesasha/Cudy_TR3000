#!/usr/bin/env python3
"""Local regression checks for Auto priority policy resolution."""

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
TEST_USER_ID = "smoke_auto_priority_user"


def assert_equal(actual: Any, expected: Any, context: str) -> None:
    if actual != expected:
        raise AssertionError(f"{context}: expected {expected!r}, got {actual!r}")


def assert_true(value: Any, context: str) -> None:
    if not value:
        raise AssertionError(context)


def resolved_policy(db_path: Path, *, user_id: str, domain: str) -> dict[str, Any] | None:
    with closing(app.connect(db_path)) as conn:
        return app.resolve_auto_candidate_policy(conn, user_id=user_id, domain=domain)


def create_test_user(db_path: Path) -> None:
    app.create_or_update_user(
        db_path,
        INVENTORY,
        user_id=TEST_USER_ID,
        display_name="Smoke Auto Priority User",
        role="user",
        password=None,
        client_ip="10.77.255.250",
        enabled=True,
        allow_no_password=True,
    )


def save_policy(db_path: Path, *, user_id: str, domain: str, servers: list[str], enabled: bool = True) -> None:
    app.save_auto_candidate_policy(
        db_path,
        INVENTORY,
        user_id=user_id,
        domain=domain,
        candidate_server_ids=servers,
        enabled=enabled,
    )


def run_priority_resolution_check(db_path: Path) -> None:
    create_test_user(db_path)

    save_policy(db_path, user_id="", domain="", servers=["proxyru"])
    policy = resolved_policy(db_path, user_id=TEST_USER_ID, domain="priority.example")
    assert_true(policy is not None, "global default policy should resolve")
    assert_equal(policy["scope"], "global_default", "global default scope")
    assert_equal(policy["candidate_server_ids"], ["proxyru"], "global default candidates")

    save_policy(db_path, user_id="", domain="priority.example", servers=["proxyus"])
    policy = resolved_policy(db_path, user_id=TEST_USER_ID, domain="priority.example")
    assert_true(policy is not None, "global domain policy should resolve")
    assert_equal(policy["scope"], "global_domain", "global domain scope")
    assert_equal(policy["candidate_server_ids"], ["proxyus"], "global domain candidates")

    save_policy(db_path, user_id=TEST_USER_ID, domain="", servers=["proxynl"])
    policy = resolved_policy(db_path, user_id=TEST_USER_ID, domain="priority.example")
    assert_true(policy is not None, "user default policy should resolve")
    assert_equal(policy["scope"], "user_default", "user default scope")
    assert_equal(policy["candidate_server_ids"], ["proxynl"], "user default candidates")

    save_policy(db_path, user_id=TEST_USER_ID, domain="priority.example", servers=["proxyde"])
    policy = resolved_policy(db_path, user_id=TEST_USER_ID, domain="priority.example")
    assert_true(policy is not None, "user domain policy should resolve")
    assert_equal(policy["scope"], "user_domain", "user domain scope")
    assert_equal(policy["candidate_server_ids"], ["proxyde"], "user domain candidates")

    save_policy(db_path, user_id=TEST_USER_ID, domain="disabled.example", servers=["proxyde"], enabled=False)
    policy = resolved_policy(db_path, user_id=TEST_USER_ID, domain="disabled.example")
    assert_true(policy is not None, "disabled user domain should fall back")
    assert_equal(policy["scope"], "user_default", "disabled user domain fallback scope")
    assert_equal(policy["candidate_server_ids"], ["proxynl"], "disabled user domain fallback candidates")


def run_all_rest_check(db_path: Path) -> None:
    save_policy(db_path, user_id=TEST_USER_ID, domain="all-rest.example", servers=["proxyde", app.AUTO_ALL_REST])
    policy = resolved_policy(db_path, user_id=TEST_USER_ID, domain="all-rest.example")
    assert_true(policy is not None, "all-rest policy should resolve")
    assert_equal(policy["candidate_server_ids"], ["proxyde", app.AUTO_ALL_REST], "all-rest raw candidates")

    with closing(app.connect(db_path)) as conn:
        defaults = app.default_auto_candidate_ids(app.server_map(conn))

    expected = ["proxyde", *[server_id for server_id in defaults if server_id != "proxyde"]]
    expanded = policy["expanded_candidate_server_ids"]
    assert_equal(expanded, expected, "all-rest expanded candidates")
    assert_true("auto" not in expanded, "all-rest expansion must not include virtual auto server")
    assert_equal(len(expanded), len(set(expanded)), "all-rest expansion should not duplicate servers")


def run_auto_winners_cache_fallback_check(db_path: Path) -> None:
    app.save_auto_cache_entry(
        db_path,
        INVENTORY,
        domain="winner-cache.example",
        selected_server_id="proxyde",
        score_ms=123,
        status="auto",
        metadata={"user_id": TEST_USER_ID, "checked_candidates": 2},
    )
    result = app.recent_auto_winners(db_path, INVENTORY, target="winner-cache.example", limit=10)
    winners = result["winners"]
    assert_equal(len(winners), 1, "auto winners should fall back to cache entry")
    assert_equal(winners[0]["winner_server_id"], "proxyde", "cache fallback winner server")
    assert_equal(winners[0]["latency_ms"], 123, "cache fallback latency")
    assert_equal(winners[0]["source"], "auto_cache", "cache fallback source")


def run_unresolved_auto_domain_falls_back_to_direct_check(db_path: Path) -> None:
    save_policy(db_path, user_id=TEST_USER_ID, domain="uncached.example", servers=["proxyde"])
    timestamp = app.now()
    with closing(app.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO user_domain_routes (user_id, domain, server_id, enabled, created_at, updated_at)
            VALUES (?, ?, 'auto', 1, ?, ?)
            """,
            (TEST_USER_ID, "uncached.example", timestamp, timestamp),
        )
        config = app.build_agent_config(
            conn,
            user_id=TEST_USER_ID,
            device={"id": "test-device", "display_name": "Test Device", "platform": "windows"},
        )
    routes = [route for route in config["domain_routes"] if route["domain"] == "uncached.example"]
    assert_equal(len(routes), 1, "uncached auto domain route should be present")
    assert_equal(routes[0]["requested_server_id"], "auto", "uncached auto requested server")
    assert_equal(routes[0]["server_id"], "direct", "uncached auto domain should fall back to direct")
    assert_true(config["warnings"], "uncached auto domain should emit warning")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cudy-auto-policy-") as tmp:
        db_path = Path(tmp) / "vpn_control.db"
        app.init_db(db_path, INVENTORY)
        run_priority_resolution_check(db_path)
        run_all_rest_check(db_path)
        run_auto_winners_cache_fallback_check(db_path)
        run_unresolved_auto_domain_falls_back_to_direct_check(db_path)
        gc.collect()

    print("Auto priority policy regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
