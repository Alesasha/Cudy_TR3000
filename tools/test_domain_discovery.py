#!/usr/bin/env python3
"""Regression checks for unknown-domain discovery queue."""

from __future__ import annotations

import sys
import tempfile
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


def assert_in(value: Any, items: list[Any], context: str) -> None:
    if value not in items:
        raise AssertionError(f"{context}: expected {value!r} in {items!r}")


def discovery_item(db_path: Path, domain: str) -> dict[str, Any]:
    with closing(app.connect(db_path)) as conn:
        return app.domain_discovery_item(conn, domain)


def run_lookup_records_direct_domain(db_path: Path) -> None:
    app.create_or_update_user(
        db_path,
        INVENTORY,
        user_id="discovery-user",
        display_name="Discovery User",
        role="user",
        password=None,
        client_ip="10.77.0.250",
        enabled=True,
        allow_no_password=True,
    )

    first = app.route_lookup(db_path, INVENTORY, user_id="discovery-user", target="https://unknown-review.example/path")
    result = first["results"][0]
    assert_equal(result["target"], "unknown-review.example", "lookup target")
    assert_equal(result["route_state"], "direct", "unknown domain should remain direct")
    assert_equal(result["server_id"], "direct", "unknown domain server")
    assert_equal(result["discovery"]["domain"], "unknown-review.example", "discovery domain")

    item = discovery_item(db_path, "unknown-review.example")
    assert_equal(item["status"], "pending", "new discovery status")
    assert_equal(item["hit_count"], 1, "new discovery hit count")
    assert_in("discovery-user", item["user_ids"], "discovery user ids")
    assert_in("10.77.0.250", item["client_ips"], "discovery client ips")

    second = app.route_lookup(db_path, INVENTORY, user_id="discovery-user", target="unknown-review.example")
    assert_equal(second["results"][0]["route_state"], "direct", "second lookup route state")
    item = discovery_item(db_path, "unknown-review.example")
    assert_equal(item["hit_count"], 2, "repeated lookup hit count")


def run_manual_record_and_mark(db_path: Path) -> None:
    with app.connect(db_path) as conn:
        item = app.record_domain_discovery(
            conn,
            domain="manual-review.example",
            user_id="discovery-user",
            client_ip="10.77.0.250",
            source="test",
            note="created by regression",
        )
    assert_equal(item["domain"], "manual-review.example", "manual discovery domain")

    marked = app.save_domain_discovery_status(
        db_path,
        INVENTORY,
        domain="manual-review.example",
        status="ignored",
        note="not needed",
    )
    assert_equal(marked["item"]["status"], "ignored", "marked discovery status")
    assert_equal(marked["item"]["note"], "not needed", "marked discovery note")

    with closing(app.connect(db_path)) as conn:
        ignored = app.domain_discovery_rows(conn, status="ignored", limit=10)
    assert_equal([item["domain"] for item in ignored], ["manual-review.example"], "ignored discovery filter")


def run_promote_to_auto_route(db_path: Path) -> None:
    result = app.promote_domain_discovery_to_auto_route(
        db_path,
        INVENTORY,
        domain="unknown-review.example",
        candidate_server_ids="proxyde, all-rest",
        note="approved by regression",
        probe_now=True,
        max_probe_candidates=2,
    )
    assert_equal(result["ok"], True, "promote result")
    assert_equal(result["route_scope"], "global_domain", "promote route scope")
    assert_equal(result["server_id"], "auto", "promote server")
    assert_equal(result["discovery"]["status"], "promoted", "promoted discovery status")
    assert_equal(result["discovery"]["note"], "approved by regression", "promoted discovery note")
    assert_equal(result["auto_candidate_policy"]["candidate_server_ids"], ["proxyde", "all-rest"], "promoted candidates")
    assert_equal(result["probe_job"]["skipped"], None, "promoted probe should not be skipped")
    assert_equal(result["probe_job"]["created"]["status"], "pending", "promoted probe job status")
    assert_equal(result["probe_job"]["created"]["domain"], "unknown-review.example", "promoted probe job domain")
    assert_equal(len(result["probe_job"]["created"]["candidate_server_ids"]), 2, "promoted probe candidate window")

    lookup = app.route_lookup(db_path, INVENTORY, user_id="discovery-user", target="unknown-review.example")
    item = lookup["results"][0]
    assert_equal(item["route_state"], "managed", "promoted lookup route state")
    assert_equal(item["requested_server_id"], "auto", "promoted lookup requested server")
    assert_equal(item["matched_rule"]["source"], "global", "promoted lookup rule source")

    with closing(app.connect(db_path)) as conn:
        route = app.row(conn, "SELECT domain, server_id FROM global_domain_routes WHERE domain = ?", ("unknown-review.example",))
    assert_equal(route["server_id"], "auto", "promoted global route")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="domain-discovery-", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "vpn_control.db"
        run_lookup_records_direct_domain(db_path)
        run_manual_record_and_mark(db_path)
        run_promote_to_auto_route(db_path)
    print("Domain discovery regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
