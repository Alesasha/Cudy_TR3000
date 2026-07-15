#!/usr/bin/env python3
"""Local regression checks for Auto priority policy resolution."""

from __future__ import annotations

import json
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


def run_stale_provider_transports_are_not_default_candidates_check(db_path: Path) -> None:
    app.save_transport_config(
        db_path,
        INVENTORY,
        server_id="proxyde",
        transport_type="http-proxy-tun",
        interface_name="proxyde",
        config={"server": "127.0.0.1", "server_port": 8080},
        enabled=True,
        source="test",
    )
    app.save_transport_config(
        db_path,
        INVENTORY,
        server_id="proxynl",
        transport_type="http-proxy-tun",
        interface_name="proxynl",
        config={"server": "127.0.0.1", "server_port": 8081},
        enabled=True,
        source="test",
    )
    with closing(app.connect(db_path)) as conn:
        conn.execute("UPDATE transport_configs SET updated_at = '2000-01-01T00:00:00+00:00' WHERE server_id = 'proxynl'")
        servers = app.server_map(conn)
        defaults = app.default_auto_candidate_ids(servers)
        user_visible = {item["id"]: item for item in app.user_servers(conn)}

    assert_true("proxyde" in defaults, "fresh provider transport should remain an Auto candidate")
    assert_true("proxynl" not in defaults, "stale provider transport should not be an Auto candidate")
    assert_true(user_visible["proxyde"]["candidate_available"], "fresh provider transport should be candidate-available")
    assert_true(not user_visible["proxynl"]["candidate_available"], "stale provider transport should be candidate-unavailable")
    assert_true(user_visible["proxynl"]["transport_stale"], "stale provider transport should be marked stale")


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


def run_uncached_auto_domain_uses_first_available_candidate_check(db_path: Path) -> None:
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
    assert_equal(routes[0]["server_id"], "proxyde", "uncached auto domain should use first available candidate")
    assert_true(config["warnings"], "uncached auto domain should report provisional selection")


def run_cached_winner_respects_effective_policy_check() -> None:
    servers = {
        "aktau": {
            "enabled": True,
            "user_visible": True,
            "candidate_available": True,
        },
        "proxyde": {
            "enabled": True,
            "user_visible": True,
            "candidate_available": True,
        },
    }
    policy = {
        "scope": "user_domain",
        "candidate_server_ids": ["aktau"],
        "expanded_candidate_server_ids": ["aktau"],
    }

    warnings: list[str] = []
    selected, cached = app.resolve_route_server(
        domain="restricted.example",
        requested_server_id="auto",
        servers=servers,
        auto_cache={"restricted.example": {"selected_server_id": "proxyde"}},
        auto_policy=policy,
        context="test route",
        warnings=warnings,
    )
    assert_equal(selected, "aktau", "cached winner outside effective policy should fall back")
    assert_equal(cached["selected_server_id"], "proxyde", "cache metadata should be preserved")
    assert_true(any("outside the effective user_domain" in item for item in warnings), "policy rejection warning")

    warnings = []
    selected, _ = app.resolve_route_server(
        domain="restricted.example",
        requested_server_id="auto",
        servers=servers,
        auto_cache={"restricted.example": {"selected_server_id": "aktau"}},
        auto_policy=policy,
        context="test route",
        warnings=warnings,
    )
    assert_equal(selected, "aktau", "cached winner inside effective policy should remain selected")
    assert_equal(warnings, [], "valid cached winner should not warn")


def run_agent_transport_plan_is_minimal_check(db_path: Path) -> None:
    for port, server_id in enumerate(["proxyde", "proxynl", "proxyus"], start=18080):
        app.save_transport_config(
            db_path,
            INVENTORY,
            server_id=server_id,
            transport_type="http-proxy-tun",
            interface_name=server_id,
            config={"server": "127.0.0.1", "server_port": port},
            enabled=True,
            source="test",
        )
    app.create_agent_device(
        db_path,
        INVENTORY,
        user_id=TEST_USER_ID,
        device_id="smoke-auto-priority-device",
        display_name="Smoke Auto Priority Device",
        platform="windows",
    )
    app.save_auto_cache_entry(
        db_path,
        INVENTORY,
        domain="cached-route.example",
        selected_server_id="proxyde",
        score_ms=111,
        status="auto",
        metadata={"user_id": TEST_USER_ID},
    )
    save_policy(db_path, user_id=TEST_USER_ID, domain="cached-route.example", servers=["proxyde"])
    timestamp = app.now()
    with app.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_domain_routes (user_id, domain, server_id, enabled, created_at, updated_at)
            VALUES (?, 'cached-route.example', 'auto', 1, ?, ?)
            """,
            (TEST_USER_ID, timestamp, timestamp),
        )
    app.create_probe_job(
        db_path,
        INVENTORY,
        domain="probe-route.example",
        candidate_server_ids=["proxynl", "proxyus"],
        user_id=TEST_USER_ID,
        assigned_device_id="smoke-auto-priority-device",
    )

    with closing(app.connect(db_path)) as conn:
        config = app.build_agent_config(
            conn,
            user_id=TEST_USER_ID,
            device={
                "id": "smoke-auto-priority-device",
                "display_name": "Smoke Auto Priority Device",
                "platform": "windows",
            },
        )
    transport_ids = sorted(item["server_id"] for item in config["transport_plan"])
    assert_equal(
        transport_ids,
        ["proxyde", "proxynl", "proxyus"],
        "agent transport_plan should include only applied route and pending probe transports",
    )


def run_auto_worker_prefers_domain_agent_check(db_path: Path) -> None:
    for port, server_id in enumerate(["proxyde", "proxynl"], start=19080):
        app.save_transport_config(
            db_path,
            INVENTORY,
            server_id=server_id,
            transport_type="http-proxy-tun",
            interface_name=server_id,
            config={"server": "127.0.0.1", "server_port": port},
            enabled=True,
            source="test",
        )
    app.save_auto_candidate_policy(
        db_path,
        INVENTORY,
        user_id=TEST_USER_ID,
        domain="worker-agent.example",
        candidate_server_ids=["proxyde", "proxynl"],
        enabled=True,
    )
    app.create_agent_device(
        db_path,
        INVENTORY,
        user_id=TEST_USER_ID,
        device_id="smoke-worker-generic-device",
        display_name="Smoke Generic Device",
        platform="windows",
    )
    app.create_agent_device(
        db_path,
        INVENTORY,
        user_id=TEST_USER_ID,
        device_id="smoke-worker-domain-device",
        display_name="Smoke Domain Device",
        platform="linux",
    )
    timestamp = app.now()
    with app.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_domain_routes (user_id, domain, server_id, enabled, created_at, updated_at)
            VALUES (?, 'worker-agent.example', 'auto', 1, ?, ?)
            """,
            (TEST_USER_ID, timestamp, timestamp),
        )
        conn.execute(
            """
            UPDATE agent_devices
            SET last_seen_at = ?, updated_at = ?
            WHERE id IN ('smoke-worker-generic-device', 'smoke-worker-domain-device')
            """,
            (timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO agent_status (device_id, status_json, reported_at)
            VALUES (?, ?, ?), (?, ?, ?)
            """,
            (
                "smoke-worker-generic-device",
                json.dumps(
                    {
                        "platform": "windows",
                        "capabilities": {"can_manage_transports": True, "can_probe": True},
                        "domain_routes": [{"domain": "other.example"}],
                    }
                ),
                timestamp,
                "smoke-worker-domain-device",
                json.dumps(
                    {
                        "platform": "linux",
                        "capabilities": {"can_manage_transports": True, "can_probe": True},
                        "domain_routes": [{"domain": "worker-agent.example"}],
                    }
                ),
                timestamp,
            ),
        )

    result = app.create_auto_probe_jobs_once(
        db_path,
        INVENTORY,
        cache_ttl_seconds=0,
        agent_stale_seconds=600,
        max_jobs=5,
        max_candidates_per_job=2,
    )
    created = result["created"]
    matching = [job for job in created if job["domain"] == "worker-agent.example"]
    assert_equal(len(matching), 1, "auto worker should create one domain probe job")
    assert_equal(
        matching[0]["assigned_device_id"],
        "smoke-worker-domain-device",
        "auto worker should assign probe to the agent that reported the domain",
    )


def run_probe_claim_requires_transport_capability_check(db_path: Path) -> None:
    timestamp = app.now()
    devices = [
        ("smoke-observer-device", False),
        ("smoke-capable-device", True),
    ]
    for device_id, can_manage in devices:
        app.create_agent_device(
            db_path,
            INVENTORY,
            user_id=TEST_USER_ID,
            device_id=device_id,
            display_name=device_id,
            platform="openwrt" if not can_manage else "windows",
        )
        with app.connect(db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_status (device_id, status_json, reported_at)
                VALUES (?, ?, ?)
                """,
                (
                    device_id,
                    json.dumps(
                        {
                            "platform": "openwrt" if not can_manage else "windows",
                            "capabilities": {"can_manage_transports": can_manage, "can_probe": True},
                        }
                    ),
                    timestamp,
                ),
            )
    job = app.create_probe_job(
        db_path,
        INVENTORY,
        domain="capability-claim.example",
        candidate_server_ids=["proxyde"],
        user_id=TEST_USER_ID,
    )
    observer_jobs = app.claim_agent_probe_jobs(
        db_path,
        INVENTORY,
        device={"id": "smoke-observer-device", "user_id": TEST_USER_ID, "platform": "openwrt"},
        limit=2,
    )
    assert_equal(observer_jobs, [], "observer must not claim a provider-transport probe job")
    capable_jobs = app.claim_agent_probe_jobs(
        db_path,
        INVENTORY,
        device={"id": "smoke-capable-device", "user_id": TEST_USER_ID, "platform": "windows"},
        limit=2,
    )
    assert_equal([item["id"] for item in capable_jobs], [job["id"]], "capable agent should claim provider probe job")


def run_auto_worker_skips_without_capable_agent_check(tmp: Path) -> None:
    db_path = tmp / "no-capable-agent.db"
    app.init_db(db_path, INVENTORY)
    create_test_user(db_path)
    app.save_transport_config(
        db_path,
        INVENTORY,
        server_id="proxyde",
        transport_type="http-proxy-tun",
        interface_name="proxyde",
        config={"server": "127.0.0.1", "server_port": 19090},
        enabled=True,
        source="test",
    )
    save_policy(db_path, user_id=TEST_USER_ID, domain="observer-only.example", servers=["proxyde"])
    app.create_agent_device(
        db_path,
        INVENTORY,
        user_id=TEST_USER_ID,
        device_id="observer-only-device",
        display_name="Observer only",
        platform="openwrt",
    )
    timestamp = app.now()
    with app.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_domain_routes (user_id, domain, server_id, enabled, created_at, updated_at)
            VALUES (?, 'observer-only.example', 'auto', 1, ?, ?)
            """,
            (TEST_USER_ID, timestamp, timestamp),
        )
        conn.execute(
            "UPDATE agent_devices SET last_seen_at = ?, updated_at = ? WHERE id = 'observer-only-device'",
            (timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO agent_status (device_id, status_json, reported_at)
            VALUES (?, ?, ?)
            """,
            (
                "observer-only-device",
                json.dumps(
                    {
                        "platform": "openwrt",
                        "capabilities": {"can_manage_transports": False, "can_probe": True},
                        "domain_routes": [{"domain": "observer-only.example"}],
                    }
                ),
                timestamp,
            ),
        )
    stale_job = app.create_probe_job(
        db_path,
        INVENTORY,
        domain="observer-only.example",
        candidate_server_ids=["proxyde"],
        user_id=TEST_USER_ID,
        assigned_device_id="observer-only-device",
    )
    result = app.create_auto_probe_jobs_once(
        db_path,
        INVENTORY,
        cache_ttl_seconds=0,
        agent_stale_seconds=600,
        max_jobs=100,
        max_candidates_per_job=1,
    )
    created_for_domain = [item for item in result["created"] if item.get("domain") == "observer-only.example"]
    assert_equal(created_for_domain, [], "worker must not create a provider probe without a capable agent")
    invalid_ids = [item["id"] for item in result.get("invalid_assignments") or []]
    assert_equal(invalid_ids, [stale_job["id"]], "worker should reconcile the stale observer assignment")
    with app.connect(db_path) as conn:
        reconciled = app.row(conn, "SELECT status, error FROM agent_probe_jobs WHERE id = ?", (stale_job["id"],))
    assert_equal((reconciled or {}).get("status"), "failed", "stale observer assignment status")
    assert_true("no longer probe-capable" in str((reconciled or {}).get("error") or ""), "stale assignment error")
    matching = [item for item in result["skipped"] if item.get("domain") == "observer-only.example"]
    assert_true(bool(matching), "worker should explain why the observer-only domain was skipped")
    assert_equal(matching[0]["reason"], "no_capable_agent", "observer-only skip reason")


def run_user_ip_auto_export_uses_cache_check(db_path: Path, tmp: Path) -> None:
    target_cidr = "203.0.113.0/24"
    cache_key = app.auto_cache_key_for_ip_route(target_cidr)
    save_policy(db_path, user_id=TEST_USER_ID, domain=cache_key, servers=["proxyde"])
    app.save_auto_cache_entry(
        db_path,
        INVENTORY,
        domain=cache_key,
        selected_server_id="proxyde",
        score_ms=222,
        status="ok",
        metadata={"user_id": TEST_USER_ID},
    )
    app.save_user_ip_route(
        db_path,
        INVENTORY,
        user_id=TEST_USER_ID,
        target_cidr=target_cidr,
        server_id="auto",
    )

    manifest = app.export_user_routes(db_path, INVENTORY, tmp / "user-routes")
    target_warnings = [warning for warning in manifest["warnings"] if target_cidr in warning]
    assert_equal(target_warnings, [], "user route export should not warn for cached auto IP route")
    matching = [route for route in manifest["exported_routes"] if route["target"] == target_cidr]
    assert_equal(len(matching), 1, "cached auto IP route should be exported")
    route = matching[0]
    assert_equal(route["requested_server_id"], "auto", "cached auto IP route requested server")
    assert_equal(route["server_id"], "proxyde", "cached auto IP route resolved server")
    assert_equal(route["interface"], "proxyde", "cached auto IP route interface")
    assert_equal(route["auto_status"], "ok", "cached auto IP route status")


def run_service_group_shares_auto_winner_check(db_path: Path) -> None:
    app.save_critical_service(
        db_path,
        INVENTORY,
        user_id="",
        service_key="smoke-suite",
        label="Smoke Suite",
        targets=["https://one.smoke.example/", "https://two.smoke.example/path"],
        routing_enabled=True,
        candidate_server_ids=["proxyde", "proxynl"],
        enabled=True,
    )
    global_key = app.service_auto_cache_key("", "smoke-suite")
    app.save_auto_cache_entry(
        db_path,
        INVENTORY,
        domain=global_key,
        selected_server_id="proxynl",
        score_ms=88,
        status="ok",
        metadata={"service_key": "smoke-suite"},
    )
    with closing(app.connect(db_path)) as conn:
        config = app.build_agent_config(
            conn,
            user_id=TEST_USER_ID,
            device={"id": "service-group-device", "display_name": "Service Group", "platform": "windows"},
        )
        specs = app.auto_probe_domain_rows(conn)
    grouped = {
        route["domain"]: route
        for route in config["domain_routes"]
        if route.get("service_key") == "smoke-suite"
    }
    assert_equal(sorted(grouped), ["one.smoke.example", "two.smoke.example"], "service group domains")
    assert_true(all(route["server_id"] == "proxynl" for route in grouped.values()), "service group should share one winner")
    assert_true(all(route["auto_cache_key"] == global_key for route in grouped.values()), "service group should share one cache key")
    matching_specs = [item for item in specs if item.get("domain") == global_key]
    assert_equal(len(matching_specs), 1, "service group should schedule one probe specification")
    assert_equal(matching_specs[0]["candidate_server_ids"], ["proxyde", "proxynl"], "service group probe candidates")

    lookup = app.route_lookup(db_path, INVENTORY, user_id=TEST_USER_ID, target="two.smoke.example")
    result = lookup["results"][0]
    assert_equal(result["server_id"], "proxynl", "service group route lookup winner")
    assert_equal(result["matched_rule"]["source"], "global_service_group", "service group route lookup source")

    app.save_critical_service(
        db_path,
        INVENTORY,
        user_id=TEST_USER_ID,
        service_key="smoke-suite",
        label="Local Smoke Suite",
        targets=["https://one.smoke.example/", "https://two.smoke.example/path"],
        routing_enabled=True,
        candidate_server_ids=["proxyde"],
        enabled=True,
    )
    local_key = app.service_auto_cache_key(TEST_USER_ID, "smoke-suite")
    app.save_auto_cache_entry(
        db_path,
        INVENTORY,
        domain=local_key,
        selected_server_id="proxyde",
        score_ms=77,
        status="ok",
        metadata={"service_key": "smoke-suite", "user_id": TEST_USER_ID},
    )
    timestamp = app.now()
    with closing(app.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO user_domain_routes (user_id, domain, server_id, enabled, created_at, updated_at)
            VALUES (?, 'one.smoke.example', 'direct', 1, ?, ?)
            ON CONFLICT(user_id, domain) DO UPDATE SET server_id = 'direct', enabled = 1, updated_at = excluded.updated_at
            """,
            (TEST_USER_ID, timestamp, timestamp),
        )
        config = app.build_agent_config(
            conn,
            user_id=TEST_USER_ID,
            device={"id": "service-group-device", "display_name": "Service Group", "platform": "windows"},
        )
    routes = {route["domain"]: route for route in config["domain_routes"]}
    assert_equal(routes["one.smoke.example"]["server_id"], "direct", "explicit user route should override local service group")
    assert_equal(routes["two.smoke.example"]["server_id"], "proxyde", "local service group should override global group")
    assert_equal(routes["two.smoke.example"]["auto_cache_key"], local_key, "local service group cache isolation")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cudy-auto-policy-", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "vpn_control.db"
        tmp_path = Path(tmp)
        app.init_db(db_path, INVENTORY)
        run_priority_resolution_check(db_path)
        run_all_rest_check(db_path)
        run_stale_provider_transports_are_not_default_candidates_check(db_path)
        run_auto_winners_cache_fallback_check(db_path)
        run_uncached_auto_domain_uses_first_available_candidate_check(db_path)
        run_cached_winner_respects_effective_policy_check()
        run_agent_transport_plan_is_minimal_check(db_path)
        run_auto_worker_prefers_domain_agent_check(db_path)
        run_probe_claim_requires_transport_capability_check(db_path)
        run_auto_worker_skips_without_capable_agent_check(tmp_path)
        run_user_ip_auto_export_uses_cache_check(db_path, tmp_path)
        run_service_group_shares_auto_winner_check(db_path)
        gc.collect()

    print("Auto priority policy regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
